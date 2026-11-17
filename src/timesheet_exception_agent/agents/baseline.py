"""Fixed-reminder baseline (Module 5 evaluation): what simple automation does without an agent.

A drop-in `act` for OutreachDriver or the graph, with no model:
- First contact: email the manager.
- Every follow-up: email the same manager a fixed reminder, up to the 3-message cap.
- It never escalates and never messages a teammate.
- A reply containing "approv" marks the exception resolved; any other reply is ignored.
  The keyword test is the point of the comparison: it also "resolves" holding notes
  ("I'll approve after my 9:30"), corrections ("please don't approve it as is") and the
  injection reply, which the Outreach agent must read correctly.
- Whatever is left at the lock goes to the coordinator, by the driver's deadline rule.

It works through the same OutreachTools, so the same code rules apply to it (caps, lock,
scoping, audit). Its tool calls are audited as the Outreach actor; the driver's
run_started entry records that the run used the baseline.
"""

import re

from timesheet_exception_agent.agents.outreach import Wake
from timesheet_exception_agent.tools.outreach import OutreachTools, ToolError

APPROVAL = re.compile(r"approv", re.IGNORECASE)

FIRST = (
    "Hi {manager}, {employee}'s timesheet needs your action ({what}) before timesheets"
    " lock at 10:00 AM today. Thank you."
)
REMINDER = (
    "Reminder: {employee}'s timesheet still needs your action ({what}) before the 10:00 AM"
    " lock."
)
WHAT = {
    "not_submitted": "submit the time",
    "not_approved": "approve the time",
    "unusual_hours": "check the hours",
}


class FixedReminderBaseline:
    def __init__(self, tools: OutreachTools) -> None:
        self.tools = tools

    def __call__(self, wake: Wake) -> str:
        info = self.tools.get_exception(wake.exception_id)
        manager = info["manager"]
        if manager is None:
            return "no manager on file; left for the coordinator"
        replies = [
            r
            for r in self.tools.check_replies(wake.exception_id)["replies"]
            if r["from_id"] == manager["id"]
        ]
        if replies and APPROVAL.search(replies[-1]["body"]):
            try:
                self.tools.update_status(
                    wake.exception_id,
                    "resolved",
                    "baseline: the manager's reply mentions approval",
                )
                return "resolved (reply mentions approval)"
            except ToolError as e:
                return f"could not resolve: {e}"
        if wake.reason not in ("first_contact", "follow_up_due"):
            return "reply ignored; waiting for the next reminder"
        template = FIRST if wake.reason == "first_contact" else REMINDER
        body = template.format(
            manager=manager["name"],
            employee=info["employee"]["name"],
            what=WHAT[info["type"]],
        )
        try:
            self.tools.send_message(wake.exception_id, manager["id"], "email", body)
        except ToolError as e:  # message cap reached: wait for the lock
            return f"no reminder sent: {e}"
        return "emailed manager" if wake.reason == "first_contact" else "sent reminder"
