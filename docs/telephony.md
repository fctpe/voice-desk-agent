# Telephony (optional)

The console demo and the evals need **no phone number and no LiveKit account** —
they run entirely locally. This document sketches how you would attach a real
phone number if you wanted Riley to answer inbound calls. It is intentionally
**optional to run**; nothing in this repo depends on it.

## How it fits together

LiveKit Agents separates the *agent* (this repo) from *media transport*. For a
phone call, LiveKit SIP bridges a PSTN/SIP trunk into a LiveKit room, and the
same `entrypoint` that powers the console demo handles the call.

```
Caller ── PSTN ──▶ SIP trunk ──▶ LiveKit SIP ──▶ LiveKit room ──▶ voice-desk-agent
        (Twilio / Telnyx)         (inbound trunk +               (this repo,
                                   dispatch rule)                 `start` mode)
```

## Steps (outline)

1. **Provision a number** with a SIP-capable provider (Twilio or Telnyx) and
   point its origination/termination at LiveKit SIP.
2. **Create an inbound trunk** in LiveKit (`lk sip inbound create ...`) with the
   provider's credentials.
3. **Add a dispatch rule** so inbound SIP calls are routed to a room and your
   agent is dispatched to it. If you set `agent_name` on the worker you can use
   explicit dispatch; otherwise automatic room dispatch works.
4. **Run the worker in server mode** instead of console:

   ```bash
   uv run python -m voice_desk start   # needs LIVEKIT_URL / _API_KEY / _API_SECRET
   ```

   The worker connects to LiveKit, and each inbound call invokes `entrypoint`
   exactly as the console demo does — same prompt, same tools, same evals.

## Notes

- **Audio tuning.** Phone audio is narrowband and noisier than a laptop mic.
  Silero VAD thresholds (activation, min-silence) are worth tuning for a trunk;
  the console defaults favour a clean microphone.
- **Caller ID.** The caller's number arrives in the SIP participant attributes,
  which is a natural input to `identify_caller` — but this repo does not wire a
  real trunk, so that path is left as an extension point.
- **Cost / latency.** A real trunk adds network hops; watch the
  `EOU -> first audio` budget in the metrics report (see the README latency
  section) once media is going over SIP.

The official reference is LiveKit's SIP documentation; this file is a map, not a
turnkey script, by design.
