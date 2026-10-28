"""A4: passages rank by RRF of the vector rank and a lexical rank over the question's concepts."""
from src.domain.drive_excerpts import rank_excerpts
from src.domain.drive_index_params import EXCERPTS_LEXICAL_WINDOW
from src.domain.drive_keys import query_concepts, word_keys


def _cid(i):
    return ("item-1", 1, i)


def _knn(n):
    return [(_cid(i), 0.10 + i * 0.001) for i in range(n)]


def _concepts(question):
    return query_concepts(question).concepts


def test_no_concept_held_leaves_the_vector_order():
    knn = _knn(5)
    keys = {cid: word_keys("Процес рецензування починається після першого читання") for cid, _ in knn}
    assert rank_excerpts(knn, keys, _concepts("review process")) == [cid for cid, _ in knn]


def test_the_vector_order_is_by_distance_whatever_the_input_order():
    knn = [(_cid(2), 0.30), (_cid(0), 0.10), (_cid(1), 0.20)]
    assert rank_excerpts(knn, {}, _concepts("review")) == [_cid(0), _cid(1), _cid(2)]


def test_a_chunk_holding_the_concepts_rises_among_the_nearest():
    knn = _knn(5)
    keys = {cid: word_keys("Acme pays the supplier in thirty days.") for cid, _ in knn}
    keys[_cid(3)] = word_keys("The review process starts after the first reading of Tom's plan.")
    ranked = rank_excerpts(knn, keys, _concepts("review process"))
    assert ranked[0] == _cid(3)
    assert ranked[1:] == [_cid(0), _cid(1), _cid(2), _cid(4)]


def test_an_id_or_number_is_lifted_among_the_nearest():
    knn = _knn(8)
    keys = {cid: word_keys("Invoices are paid by Acme within thirty days.") for cid, _ in knn}
    keys[_cid(6)] = word_keys("Invoice 1847 was paid on 2026-10-01.")
    ranked = rank_excerpts(knn, keys, _concepts("invoice 1847"))
    # every chunk holds "invoice"; only chunk 6 holds the number, and it climbs over the four just above it
    assert ranked.index(_cid(6)) == 2
    assert ranked[:2] == [_cid(0), _cid(1)]


def test_a_number_held_by_one_chunk_alone_leads():
    knn = _knn(8)
    keys = {cid: word_keys("Acme pays suppliers within thirty days.") for cid, _ in knn}
    keys[_cid(6)] = word_keys("Invoice 1847 was paid on 2026-10-01.")
    assert rank_excerpts(knn, keys, _concepts("1847"))[0] == _cid(6)


def test_tied_lexical_scores_share_the_average_rank():
    # chunk 1 holds both concepts (lexical rank 1); chunks 0 and 2 hold one each and share rank 2.5. With ranks 2 and 3
    # handed out in vector order instead, chunk 0 would stay first.
    knn = [(_cid(0), 0.10), (_cid(1), 0.11), (_cid(2), 0.12)]
    keys = {_cid(0): word_keys("review"), _cid(1): word_keys("review process"), _cid(2): word_keys("review")}
    assert rank_excerpts(knn, keys, _concepts("review process")) == [_cid(1), _cid(0), _cid(2)]


def test_a_chunk_with_no_concept_gets_no_lexical_rank():
    knn = [(_cid(0), 0.10), (_cid(1), 0.11), (_cid(2), 0.12)]
    keys = {_cid(0): (), _cid(1): word_keys("review"), _cid(2): word_keys("review")}
    assert rank_excerpts(knn, keys, _concepts("review")) == [_cid(1), _cid(2), _cid(0)]


def test_only_the_nearest_chunks_vote():
    n = EXCERPTS_LEXICAL_WINDOW + 5
    knn = _knn(n)
    far = _cid(EXCERPTS_LEXICAL_WINDOW)          # 31st nearest
    keys = {far: word_keys("The review process starts here.")}
    ranked = rank_excerpts(knn, keys, _concepts("review process"))
    assert ranked == [cid for cid, _ in knn]


def test_the_last_voting_chunk_still_votes():
    n = EXCERPTS_LEXICAL_WINDOW + 5
    knn = _knn(n)
    last = _cid(EXCERPTS_LEXICAL_WINDOW - 1)
    keys = {last: word_keys("The review process starts here.")}
    assert rank_excerpts(knn, keys, _concepts("review process"))[0] == last


def test_every_hit_is_ranked_once():
    knn = _knn(4) + [(_cid(1), 0.5)]
    assert rank_excerpts(knn, {}, ()) == [_cid(0), _cid(1), _cid(2), _cid(3)]
