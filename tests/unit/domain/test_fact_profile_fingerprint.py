"""What a profile was made from and whether it still holds (fact profile RFC §4.1, §4.2, §4.4): the profiler's user
message bytes, the fingerprint, the derivation version and `classify_profile`, including the abandon rule."""
import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain import fact_domains
from src.domain.entities import FactDomain, FactEntity, FactState, FactType
from src.domain.fact_profile_fingerprint import (
    ABANDON_AFTER_FAILURES, DERIVE_VERSION, ProfileStatus, build_profiler_user_message, classify_profile,
    failure_fields, metadata_lines, profile_derivation, profile_fingerprint, registry_digest,
)

TEXT = "Alex's passport expires 2031-03-04."
DERIVATION = profile_derivation("reg", "atlas", False)


def fact(text=TEXT, metadata=None, **over):
    return FactEntity(account_id="acc", created_by_user_id="usr", lineage_id="lin", text=text, type=FactType.EVENT,
                      metadata=metadata or {}, **over)


def profiled(text=TEXT, metadata=None, **over):
    fields = dict(profile_fingerprint=profile_fingerprint(text, metadata), profile_derivation=DERIVATION)
    fields.update(over)
    return fact(text, metadata, **fields)


# ------------------------------------------------------------------------------------------------ the message

def test_a_fact_without_metadata_is_the_text_alone():
    assert build_profiler_user_message(TEXT, None) == f"Fact:\n{TEXT}"
    assert build_profiler_user_message(TEXT, {}) == f"Fact:\n{TEXT}"


def test_metadata_lines_follow_in_sorted_key_order_with_compact_json():
    metadata = {"valid_until": "2026-12-09", "holder": {"name": "Clara Moreira", "born": "1982-07-14"},
                "countries": ["Portugal", "Italy"], "renewal_days": 22, "urgent": True, "note": None}
    assert build_profiler_user_message("Clara's passport is valid until 2026-12-09.", metadata) == (
        "Fact:\nClara's passport is valid until 2026-12-09.\nMetadata:\n"
        'countries: ["Portugal","Italy"]\n'
        'holder: {"born":"1982-07-14","name":"Clara Moreira"}\n'
        "note: null\n"
        "renewal_days: 22\n"
        "urgent: true\n"
        "valid_until: 2026-12-09")


def test_a_map_value_prints_the_same_whatever_its_key_order():
    assert metadata_lines({"m": {"a": 1, "b": 2}}) == metadata_lines({"m": {"b": 2, "a": 1}})


def test_non_ascii_is_printed_as_is_and_a_timestamp_as_text():
    when = datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert metadata_lines({"route": "Lisboa → Oporto", "at": when}) == [f'at: "{when}"', "route: Lisboa → Oporto"]


def test_the_fingerprint_is_the_sha256_of_the_message():
    metadata = {"expires": "2031-03-04"}
    message = build_profiler_user_message(TEXT, metadata).encode("utf-8")
    assert profile_fingerprint(TEXT, metadata) == hashlib.sha256(message).hexdigest()


def test_the_fingerprint_moves_with_text_and_metadata_only():
    base = profile_fingerprint(TEXT, {"k": "v"})
    assert profile_fingerprint(TEXT + " ", {"k": "v"}) != base
    assert profile_fingerprint(TEXT, {"k": "w"}) != base
    assert profile_fingerprint(TEXT, {"k": "v", "j": "v"}) != base


# ------------------------------------------------------------------------------------------------ derivation version

def test_the_derivation_string_names_every_code_input():
    assert profile_derivation("r1", "a1", True) == f"{DERIVE_VERSION}|r1|a1|1"
    assert profile_derivation("r1", "a1", False) == f"{DERIVE_VERSION}|r1|a1|0"


def test_the_registry_digest_is_stable_and_follows_the_registry_and_its_order(monkeypatch):
    digest = registry_digest()
    assert digest == registry_digest() and len(digest) == 16
    import src.domain.fact_profile_fingerprint as module
    monkeypatch.setattr(module, "FACT_DOMAINS", tuple(reversed(fact_domains.FACT_DOMAINS)))
    assert registry_digest() != digest
    first = fact_domains.FACT_DOMAINS[0]
    monkeypatch.setattr(module, "FACT_DOMAINS", (replace(first, use_area=not first.use_area),
                                                 *fact_domains.FACT_DOMAINS[1:]))
    assert registry_digest() != digest


# ------------------------------------------------------------------------------------------------ classification

def test_a_fact_never_profiled_is_missing():
    assert classify_profile(fact(), DERIVATION) is ProfileStatus.MISSING


def test_a_profile_of_the_current_text_and_derivation_is_fresh():
    assert classify_profile(profiled(), DERIVATION) is ProfileStatus.FRESH


def test_a_tags_lifecycle_or_old_domain_change_keeps_it_fresh():
    changed = profiled().model_copy(update=dict(tags=["travel"], state=FactState.STALE, domain=FactDomain.LEGAL,
                                                reported_date=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert classify_profile(changed, DERIVATION) is ProfileStatus.FRESH


def test_a_content_version_carrying_the_old_profile_is_stale_input():
    old = profiled()
    version = old.model_copy(update=dict(text="Alex's passport expires 2031-03-05."))
    assert classify_profile(version, DERIVATION) is ProfileStatus.STALE_INPUT
    assert classify_profile(old.model_copy(update=dict(metadata={"k": "v"})), DERIVATION) is ProfileStatus.STALE_INPUT


def test_a_code_input_change_is_stale_derivation():
    assert classify_profile(profiled(), profile_derivation("reg", "atlas", True)) is ProfileStatus.STALE_DERIVATION
    assert classify_profile(profiled(profile_derivation=None), DERIVATION) is ProfileStatus.STALE_DERIVATION


@pytest.mark.parametrize("stored", [None, "old-fingerprint"])
def test_three_failures_on_the_current_content_abandon_the_fact(stored):
    f = profiled(profile_fingerprint=stored, profile_failures=ABANDON_AFTER_FAILURES,
                 profile_failed_fingerprint=profile_fingerprint(TEXT, None))
    assert classify_profile(f, DERIVATION) is ProfileStatus.ABANDONED


def test_fewer_failures_do_not_abandon():
    f = fact(profile_failures=ABANDON_AFTER_FAILURES - 1, profile_failed_fingerprint=profile_fingerprint(TEXT, None))
    assert classify_profile(f, DERIVATION) is ProfileStatus.MISSING


def test_a_content_change_ends_the_abandonment():
    f = fact(profile_failures=ABANDON_AFTER_FAILURES, profile_failed_fingerprint=profile_fingerprint(TEXT, None))
    assert classify_profile(f.model_copy(update=dict(text=TEXT + " Renewed.")), DERIVATION) is ProfileStatus.MISSING


def test_failures_count_on_one_fingerprint_and_restart_on_another():
    assert failure_fields(None, None, "fp1") == {"profile_failures": 1, "profile_failed_fingerprint": "fp1"}
    assert failure_fields(1, "fp1", "fp1") == {"profile_failures": 2, "profile_failed_fingerprint": "fp1"}
    assert failure_fields(2, "fp1", "fp2") == {"profile_failures": 1, "profile_failed_fingerprint": "fp2"}
