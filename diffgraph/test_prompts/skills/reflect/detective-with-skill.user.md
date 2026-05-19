---
# Skill-mounted detective — `reflect` skill adds the `reflect`
# tool + guidance to bank concerns / hypotheses / closed
# questions across the investigation. The intended pattern: open
# Q-IDs for each concern raised by the briefing, advance one
# hypothesis at a time via probes, resolve Q-IDs as info comes
# in, surface new questions when surprising info opens fresh
# threads. Used by SKILL-003-with scenario; A/B partner of
# detective.user.md.
skills:
  - reflect
data:
  briefing:
    type: string
    description: "Short narrative of the crime — the setup the detective derives concerns from."
---
## Case briefing

{{ briefing }}

## Your task

Run the investigative loop described in the methodology — derive
concerns from the briefing, form hypotheses about who's
responsible, probe selectively to narrow the candidate set, and
accuse the unique match via `answer(text="<name>")`.
