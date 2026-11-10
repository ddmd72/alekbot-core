---
category: output_format
class: output_format
metadata:
  description: 'FactProfilerAgent: the answer fields and rules (FACT_PROFILE_RFC §4.3)'
  override_by:
  - SYSTEM
  - AGENT
  source: FACT_PROFILE_RFC §4.3, §4.5, §4.6, variant B
token_id: OUTPUT_FORMAT_FACT_PROFILER
---
OUTPUT FORMAT — what the answer is for

Your answer is one JSON object and nothing else. A schema fixes every field's name and type and the allowed values of
every closed field, so no field can be left out and no closed field can take another value. Code reads the object as
it is: it checks each field, drops what fails and counts every drop. The whole answer is also stored, so code can
recompute the domains later without asking you again. A profile marked needs_review is still stored and used; its open
points are kept so the user can be asked about them.

Fields:
- label_words: a list of strings, the labels LABEL WORDS asks for. Code embeds the kept labels together as one search
  vector.
- time_role: one of event, state, validity, measurement, timeless (TIME ROLE).
- time_points: a list of objects, each with from, to, precision and source (TIME POINTS). to is an empty string for a
  single date; a date printed with no year is written without one in from and to.
- places: a list of objects, each with city, country and source (PLACES).
- people: a list of objects, each with name, role and source (PEOPLE).
- quality: one of confident, needs_review (QUALITY).
- review_items: a list of objects, each with field (one of people, time_points, places) and mention (QUALITY); empty
  when quality is confident.
- tax_or_fine: one of none, tax, fine.
- user_identity, pet, state_or_law, money_flow: true or false.
- about_person: one of none, relative, other.
- subject: one value of SUBJECT ENUM below.
- use_areas: a list of values of USE-AREA ENUM below.
- kinds: a list of values among preference, constraint, ownership, document, incident, identifier, intention.
- sensitivity: one of ordinary, discreet, intimate.
The last ten are the domain answers: code computes the domains from them, and stores user_identity, kinds and
sensitivity as you give them.
