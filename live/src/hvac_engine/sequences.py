"""Sequence definitions. Templates use str.format with: first, co, issue, season, offer, days."""
from __future__ import annotations

from .domain import Channel, Step

SEQUENCES: dict[str, list[Step]] = {
    "speed_to_lead": [
        Step(0, Channel.SMS, "Hi {first}, thanks for contacting {co}! I'm the virtual assistant. What's going on with your {issue}? Reply here and I can get you booked. Reply STOP to opt out."),
        Step(0, Channel.EMAIL, "Thanks for reaching out to {co}, {first}. We got your request about your {issue} and will help you right away."),
        Step(4, Channel.SMS, "{first}, still need help? We have openings this week. Reply YES and I'll find a time."),
        Step(24, Channel.SMS, "Hi {first}, {co} here. Want me to hold a tech slot for you? Just reply with a day that works."),
        Step(72, Channel.EMAIL, "{first}, a quick note from {co}: we can usually get a tech out within 48 hours. Reply to book."),
        Step(168, Channel.SMS, "Last check-in from {co}, {first}. Reply BOOK anytime and we'll get you on the schedule."),
    ],
    "estimate_reactivation": [
        Step(0, Channel.SMS, "Hi {first}, {co} here. Still thinking about the estimate we sent? Happy to answer questions or adjust the scope. Reply STOP to opt out."),
        Step(72, Channel.EMAIL, "{first}, our estimate is still available. Financing options can bring the monthly cost down. Reply to chat."),
        Step(168, Channel.SMS, "{first}, this week we can add $50 off your install if you approve. Interested?"),
    ],
    "db_reactivation": [
        Step(0, Channel.SMS, "Hi {first}, it's {co}. It's been a while since your last service. Want a tune-up booked? Reply YES. Reply STOP to opt out."),
        Step(96, Channel.EMAIL, "{first}, regular maintenance prevents breakdowns. Reply to schedule your tune-up with {co}."),
    ],
    "fill_calendar": [
        Step(0, Channel.SMS, "Hi {first}, {co} has openings on {days}. Book your {season} tune-up this week for {offer}. Reply YES to claim a slot. Reply STOP to opt out."),
        Step(0, Channel.EMAIL, "{first}, we have a few open slots on {days}. {offer} on {season} tune-ups. Reply to book."),
    ],
}

SEQUENCES["review_request"] = [
    Step(2, Channel.SMS, "Thanks for choosing {co}, {first}! If we earned it, a quick review helps a lot.{link}"),
]

# Sequences that never stop on a FieldPulse state change (they are triggered BY one).
TRANSACTIONAL = frozenset({"review_request"})
# Everything that must stop once the lead has booked / converted / completed / paid.
STOPPABLE = frozenset(SEQUENCES) - TRANSACTIONAL
# Counts against the weekly marketing cap.
MARKETING_SEQUENCES = frozenset(SEQUENCES) - {"speed_to_lead"} - TRANSACTIONAL

SEASON_BY_MONTH = {
    12: "winter heating", 1: "winter heating", 2: "winter heating",
    3: "spring AC", 4: "spring AC", 5: "spring AC",
    6: "summer AC", 7: "summer AC", 8: "summer AC",
    9: "fall furnace", 10: "fall furnace", 11: "fall furnace",
}
