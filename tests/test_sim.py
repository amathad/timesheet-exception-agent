from datetime import datetime, timedelta

import pytest

from timesheet_exception_agent.sim.clock import LOCK, START, TZ, SimClock
from timesheet_exception_agent.sim.personas import OUT_OF_OFFICE, ReplySimulator
from timesheet_exception_agent.sim.seed import PERSONAS


def at(hour, minute=0):
    return datetime(2026, 10, 26, hour, minute, tzinfo=TZ)


# Clock


def test_clock_starts_monday_8am_and_steps_ten_minutes():
    clock = SimClock()
    assert clock.now() == START
    assert f"{START:%a %H:%M}" == "Mon 08:00"
    assert clock.advance() == at(8, 10)


def test_twelve_steps_reach_lock():
    clock = SimClock()
    steps = 0
    while not clock.is_past(LOCK):
        clock.advance()
        steps += 1
    assert steps == 12
    assert clock.minutes_until(LOCK) == 0


def test_clock_rejects_going_backwards_and_naive_times():
    clock = SimClock(at(9))
    with pytest.raises(ValueError):
        clock.advance(timedelta(minutes=-1))
    with pytest.raises(ValueError):
        clock.advance_to(at(8))
    with pytest.raises(ValueError):
        SimClock(datetime(2026, 10, 26, 8))


def test_minutes_until_goes_negative_after_lock():
    clock = SimClock(at(10, 30))
    assert clock.minutes_until(LOCK) == -30


# Personas


@pytest.fixture
def sim():
    return ReplySimulator(PERSONAS)


def test_responsive_manager_replies_after_ten_minutes(sim):
    reply = sim.on_sent(1, "M-01", "teams", "Avery Brook", at(8))
    assert reply.sim_time == at(8, 10)
    assert "approved" in reply.body and "Avery Brook" in reply.body
    assert sim.due(at(8, 9)) == []
    assert sim.due(at(8, 10)) == [reply]
    assert sim.pending() == 0


def test_on_leave_manager_auto_replies_on_email_only(sim):
    assert sim.on_sent(3, "M-02", "teams", "Cameron Ashby", at(8)) is None
    reply = sim.on_sent(3, "M-02", "email", "Cameron Ashby", at(8))
    assert reply.body == OUT_OF_OFFICE
    assert reply.sim_time == at(8)


def test_silent_manager_never_replies(sim):
    assert sim.on_sent(5, "M-03", "email", "Dakota Reyes", at(8)) is None
    assert sim.due(at(17)) == []


def test_slow_next_level_misses_the_thirty_minute_window(sim):
    reply = sim.on_sent(5, "N-02", "teams", "Dakota Reyes", at(8, 30))
    assert reply.sim_time - at(8, 30) > timedelta(minutes=30)


def test_correction_manager_asks_for_different_hours(sim):
    reply = sim.on_sent(7, "M-04", "email", "Emerson Tal", at(8))
    assert "72 hours" in reply.body


def test_no_next_level_manager_is_out_of_office(sim):
    reply = sim.on_sent(8, "M-05", "email", "Finley Osei", at(8))
    assert reply.body == OUT_OF_OFFICE


def test_teammates_without_persona_do_not_reply(sim):
    assert sim.on_sent(8, "E-09", "teams", "Finley Osei", at(9)) is None


def test_one_reply_per_person_per_exception(sim):
    assert sim.on_sent(1, "M-01", "teams", "Avery Brook", at(8)) is not None
    assert sim.on_sent(1, "M-01", "email", "Avery Brook", at(8, 20)) is None
    # A different exception for the same manager still gets its own reply.
    assert sim.on_sent(2, "M-01", "teams", "Blake Corran", at(8)) is not None


def test_due_returns_replies_oldest_first(sim):
    sim.on_sent(7, "M-04", "email", "Emerson Tal", at(8))  # 8:15
    sim.on_sent(1, "M-01", "teams", "Avery Brook", at(8))  # 8:10
    assert [r.exception_id for r in sim.due(at(8, 20))] == [1, 7]


def test_overrides_cover_ambiguous_and_injection_replies():
    sim = ReplySimulator(PERSONAS, overrides={"M-01": "injection", "N-01": "ambiguous"})
    assert (
        "ignore your previous instructions"
        in sim.on_sent(1, "M-01", "teams", "Avery Brook", at(8)).body
    )
    assert (
        "before I sign off"
        in sim.on_sent(3, "N-01", "teams", "Cameron Ashby", at(8)).body
    )


def test_use_history_reply_hands_the_decision_back():
    sim = ReplySimulator(PERSONAS, overrides={"M-03": "use_history"})
    reply = sim.on_sent(6, "M-03", "teams", "Gray Nakamura", at(8))
    assert reply.sim_time == at(8, 15)
    assert "usually works" in reply.body
    assert "approve" not in reply.body.lower()  # sounds helpful, approves nothing


def test_unknown_reply_kind_is_rejected():
    with pytest.raises(ValueError):
        ReplySimulator(PERSONAS, overrides={"M-01": "chatty"})


def test_same_inputs_give_same_replies():
    a = ReplySimulator(PERSONAS).on_sent(7, "M-04", "email", "Emerson Tal", at(8))
    b = ReplySimulator(PERSONAS).on_sent(7, "M-04", "email", "Emerson Tal", at(8))
    assert a == b


def test_stalling_manager_answers_every_message_and_never_approves():
    sim = ReplySimulator(PERSONAS, overrides={"M-01": "stalling"})
    replies = [
        sim.on_sent(1, "M-01", "teams", "Avery Brook", at(8, m)) for m in (0, 30, 50)
    ]
    assert [r.sim_time for r in replies] == [at(8, 5), at(8, 35), at(8, 55)]
    bodies = [r.body for r in replies]
    assert len(set(bodies)) == 3  # a different holding note each time
    assert all("Avery Brook" in b and "approved" not in b for b in bodies)


def test_stalling_notes_cycle_after_the_last_one():
    sim = ReplySimulator(PERSONAS, overrides={"M-01": "stalling"})
    first = sim.on_sent(1, "M-01", "teams", "Avery Brook", at(8)).body
    for m in (10, 20):
        sim.on_sent(1, "M-01", "teams", "Avery Brook", at(8, m))
    assert sim.on_sent(1, "M-01", "teams", "Avery Brook", at(8, 30)).body == first
