#!/usr/bin/env python3
"""
POC: callback identity round trip. A tiny local HTTP server answers an
inbound Twilio call with <Say>+<Hangup>, then originates an outbound call
back to the same number with answering-machine detection enabled. Times
the gap and logs the AMD result. Expose /auth via ngrok and point the
Twilio number's Voice webhook at it for this spike only.

Built on Quart (not Flask) — this repo already depends on Quart
(`quart>=0.18.0` in requirements.txt) for its real web app and does not
carry Flask at all; Quart's API is Flask-compatible (`@app.route`, the
`request` object) but ASGI/async-native, matching this repo's asyncio POC
convention. Confirmed live against the installed Quart==0.20.0 before
writing this: `Request.form` is an `async def` property (`await
request.form`, not a plain attribute access like Flask), and
`Quart.run()` is a synchronous method that manages its own event loop
internally (same call shape as Flask's `app.run(...)` — no
`asyncio.run(app.run_task(...))` wrapper needed).

IMPORTANT — Charles Proxy MITM. This dev machine runs Charles Proxy as
the system HTTPS proxy, which breaks default TLS certificate verification
for outbound HTTPS calls from Python (the `twilio.rest.Client` uses
`requests` under the hood) — the same issue that broke `websockets.connect()`
in Tasks 1/3/5 of this spike plan (see
reference_charles_proxy_python_ssl.md). Run this script with the proxy
bypassed:

    NO_PROXY='*' python3 scripts/voice/test_callback_roundtrip_poc.py

Without it, `client.calls.create(...)` in /auth will fail with
`SSL: CERTIFICATE_VERIFY_FAILED` rather than a Twilio API error.
"""
import os
import sys
import time
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from quart import Quart, request, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

app = Quart(__name__)
ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
FROM_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
BOUND_NUMBER = os.environ["SPIKE_BOUND_NUMBER"]  # the owner's real phone, E.164 — set per-run, not in .env
PUBLIC_BASE_URL = os.environ["SPIKE_NGROK_URL"]  # e.g. https://abcd1234.ngrok.io

client = Client(ACCOUNT_SID, AUTH_TOKEN)
call_log = {}


@app.route("/auth", methods=["POST"])
async def auth_webhook():
    form = await request.form
    caller = form.get("From")
    inbound_ts = time.monotonic()
    print(f"[{datetime.now().isoformat()}] inbound call from {caller}")
    if caller != BOUND_NUMBER:
        print(f"REJECTED: {caller} is not the bound number {BOUND_NUMBER}")
        vr = VoiceResponse()
        vr.reject()
        return Response(str(vr), mimetype="text/xml")

    vr = VoiceResponse()
    vr.say("Calling you back.")
    vr.hangup()

    outbound = client.calls.create(
        to=BOUND_NUMBER,
        from_=FROM_NUMBER,
        url=f"{PUBLIC_BASE_URL}/answer",
        machine_detection="DetectMessageEnd",
        async_amd=False,  # block on AMD result before /answer fires, for this spike's simplicity
        status_callback=f"{PUBLIC_BASE_URL}/status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )
    call_log[outbound.sid] = {"inbound_ts": inbound_ts}
    print(f"originated callback sid={outbound.sid}")
    return Response(str(vr), mimetype="text/xml")


@app.route("/answer", methods=["POST"])
async def answer_webhook():
    form = await request.form
    call_sid = form.get("CallSid")
    # with MachineDetection=DetectMessageEnd (used below), AnsweredBy is one of:
    # "human" | "machine_end_beep" | "machine_end_silence" | "machine_end_other" | "fax" | "unknown"
    # — confirmed live 2026-09-20 against twilio.com/docs/voice/answering-machine-detection.
    # (MachineDetection=Enable instead would report "machine_start", not used here.)
    answered_by = form.get("AnsweredBy")
    answered_ts = time.monotonic()
    inbound_ts = call_log.get(call_sid, {}).get("inbound_ts")
    gap_s = (answered_ts - inbound_ts) if inbound_ts else None
    print(f"[{datetime.now().isoformat()}] answer webhook: AnsweredBy={answered_by} "
          f"gap_since_inbound={gap_s}")

    vr = VoiceResponse()
    if answered_by and answered_by.startswith("machine"):
        print("MACHINE DETECTED — hanging up, no session opened")
        vr.hangup()
    else:
        vr.say("This is where the real session would start.")
    return Response(str(vr), mimetype="text/xml")


@app.route("/status", methods=["POST"])
async def status_callback():
    form = await request.form
    print(f"[status] {form.get('CallStatus')} sid={form.get('CallSid')}")
    return ("", 204)


if __name__ == "__main__":
    app.run(port=8766)
