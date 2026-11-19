"""Guardrail switches and the deterministic reply screen (Module 6).

Guardrails: every rule the Outreach tools enforce has a name, so a refusal can be counted
per guardrail (audit action `tool_refused`). Five of them can be switched off one at a
time for the guardrail ablation. A switched-off guardrail still runs its check: instead of
refusing, the tool goes ahead and audits `guardrail_bypassed` with what it would have
refused, so the ablation counts what got through. Everything stays on by default; only
the evaluation script turns one off.

Reply screen: replies are untrusted. `screen_reply` flags a reply that looks like it gives
the agent instructions (role or system wording, "ignore your instructions", tool or status
names), asks for bulk action ("approve everyone", "mark every open exception"), asks for
secrecy, or names employees the message wasn't about. Deterministic patterns, no model, so
it can't be talked out of a flag. A flagged reply doesn't count as a manager decision:
`update_status` refuses resolved and correction_requested while the latest reply on the
exception is flagged, and the agent hands it to the coordinator instead. Flags favour
caution: a false positive costs the coordinator a look; a miss could cost wrong pay.
"""

import re
from dataclasses import dataclass, fields

# Names used in tool_refused / guardrail_bypassed audit entries.
SCOPE = "scope"  # sends and status changes only on the exception being worked
RECIPIENT = "recipient"  # manager, next-level manager or teammate only
MESSAGE_CAP = "message_cap"  # at most 3 messages per person per exception
ESCALATION_CUTOFF = "escalation_cutoff"  # an engaged manager is not escalated before 9:00
CITATIONS = "citations"  # cite only sections retrieved for this exception
LOCK = "lock"  # no approvals after the 10:00 lock
TRANSITION = "transition"  # legal status moves only; agent never closes or approves
INJECTION_SCREEN = "injection_screen"  # a flagged reply is not a decision
INPUT = "input"  # malformed arguments (empty body, unknown channel, ...)


@dataclass(frozen=True)
class Guardrails:
    """Which switchable guardrails are on. Recipient, lock and transition rules are not
    switchable: turning them off would let the agent pay people wrongly by design."""

    scope: bool = True
    citations: bool = True
    escalation_cutoff: bool = True
    message_cap: bool = True
    injection_screen: bool = True

    def off(self) -> list[str]:
        return [f.name for f in fields(self) if not getattr(self, f.name)]

    @classmethod
    def without(cls, *names: str) -> "Guardrails":
        unknown = set(names) - set(SWITCHABLE)
        if unknown:
            raise ValueError(f"unknown guardrails {sorted(unknown)}; choose from {SWITCHABLE}")
        return cls(**{n: False for n in names})


SWITCHABLE = tuple(f.name for f in fields(Guardrails))
ALL_ON = Guardrails()


# Reply screen

INSTRUCTION = [
    (r"\bignore\b[^.]{0,30}\b(instructions?|rules?|prompt|guidelines|above|previous)\b",
     "tells the reader to ignore instructions"),
    (r"\bdisregard\b|\boverride\b|\bnew instructions?\b|\bjailbreak\b",
     "tries to override instructions"),
    (r"\b(system|admin|administrator|developer)\s+(note|message|prompt|instruction|override)",
     "uses system or admin wording"),
    (r"\b(note|message|instruction)s?\s+(to|for)\s+(the\s+)?(assistant|ai|agent|bot|model)\b",
     "addresses the assistant"),
    (r"\b(you are|act as|pretend to be|you're)\s+(now\s+)?(an?\s+|the\s+)?(\w+\s+)?"
     r"(assistant|ai|admin|administrator|system|coordinator)\b",
     "tries to change the reader's role"),
    (r"\b(update_status|send_message|record_manager_leave|check_replies|needs_fallback"
     r"|correction_requested)\b",
     "names agent tools or statuses"),
]

BULK = [
    (r"\b(approve|mark|set|close|resolve|enter|submit)\w*\b[^.]{0,60}"
     r"\b(all|every|each|everyone|everybody|entire|whole|any|other)\b"
     r"[^.]{0,40}\b(exceptions?|timesheets?|team|employees|staff|reports|people|hours)\b",
     "asks for action on many exceptions"),
    (r"\b(all|every|each|entire|whole)\b[^.]{0,40}"
     r"\b(exceptions?|timesheets?|team|employees|staff|reports)\b[^.]{0,40}"
     r"\b(approved?|resolved?|closed?|marked)\b",
     "asks for action on many exceptions"),
    (r"\b(approve|approving)\s+(everyone|everybody|them all|all of them)\b"
     r"|\b(everyone|everybody) else\b",
     "asks for action on many exceptions"),
]

SECRECY = [
    (r"\bdo(n't| not)\s+(tell|notify|inform|cc|copy|loop in|escalate|mention|flag)\b",
     "asks to keep others out"),
    (r"\bwithout\s+(telling|asking|notifying|checking|informing|flagging)\b",
     "asks to skip checks"),
    (r"\b(no need|not necessary)\s+to\s+(check|verify|confirm|tell|notify)\b",
     "asks to skip checks"),
]

PATTERNS = [(re.compile(p, re.IGNORECASE), why) for p, why in INSTRUCTION + BULK + SECRECY]


def screen_reply(body: str, *, about: list[str], roster: list[str]) -> list[str]:
    """Why a reply looks like an injection attempt, or [] if it doesn't.

    `about`: the names of the employees the message was about (one, or several for a merged
    message). `roster`: every employee name known to the coordinator's state, so naming
    someone else is caught (managers sign their own names, so only employees count)."""
    reasons = []
    for pattern, why in PATTERNS:
        if pattern.search(body) and why not in reasons:
            reasons.append(why)
    allowed = {n.lower() for n in about}
    named = []
    for name in roster:
        if name.lower() in allowed:
            continue
        first = name.split()[0]
        if re.search(rf"\b{re.escape(name)}\b", body, re.IGNORECASE) or re.search(
            rf"\b{re.escape(first)}'s\b", body, re.IGNORECASE
        ):
            named.append(name)
    if named:
        reasons.append(f"names employees the message wasn't about: {', '.join(named)}")
    return reasons
