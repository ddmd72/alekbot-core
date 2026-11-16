---
category: consolidation_email
class: email_candidates
metadata:
  description: 'ConsolidationAgent Stage 3 on email cards (email index v2 §6.7): what
    an email candidate is (a card a model wrote from mail other people sent, or the
    older one-sentence form), which keys the mail prints and which a model wrote, the
    date rule, whose fact it is, and how many facts a mail yields'
  override_by:
  - AGENT
source_file: prompts_snapshot/tokens/system/CONSOLIDATION_EMAIL_CANDIDATES.groovy
token_id: CONSOLIDATION_EMAIL_CANDIDATES
uploaded_by: local_script
---
/**
 * Email candidates (Stage 3). In this pass the message is not a conversation: after one header line it lists the
 * owner's mail, one numbered JSON object per mail. Step 1 of the cognitive process (Parse conversation -> list of
 * candidate facts) reads this list instead: the candidate facts are what these mails record about the owner's life.
 */

@critical
rule Email_Candidates() {

    purpose: "Memory is what the owner's assistant knows about his life when it answers him later. Mail is one of its sources: matters of his life reach him by mail, and he expects his assistant to know them without telling it. Most mail records nothing of that kind; code has already left out mail with nothing in it, but the selection still contains noise. Decide each mail on its worth, with the same rules as any candidate: search before you create, and discard what fails them."

    where_it_comes_from: "None of this is the owner's own words. Each mail was written by its sender, and for most mails a model then read the mail and wrote a card of it; code checked the card against the mail and sends here only what passed. So a card tells you what a mail says, not what the owner said or did: record what the mail shows about his life, as the mail's statement. A mail that shows an existing fact still holds is handled by the extension rule like any email."

    the_two_forms: {
        card: "A candidate with email_id, date, from, subject, doc_type and about is a card. A key with nothing to show is left out, so any other key may be absent."
        line: "A candidate with a fact key is the older one-sentence form: a classifier's one sentence about the mail (fact), its category and tags, and the mail's date, from, subject, attachment names and metadata. It is sent for mail that has no readable card. Read it as before; the date rule below applies to it too."
    }

    printed_keys: {
        why: "These keys hold what the mail itself prints, each checked against the mail by code. They are the ones to trust for names, numbers and dates."
        times: "The dates the mail prints, each verified against it: label says what the date is (issued, event, valid_from, valid_until, due, period_end or other), from is its first day and to its last day (to is present only for a span). They say when things happen or hold."
        printed_names: "The named things the mail prints: name as printed, type (place, organization, person, event, product, work or other)."
        printed_values: "The values the mail prints (references, numbers, amounts): label says what it is, value is as printed."
        people: "The people the mail names, other than the owner: each a name as the mail prints it, or the words the mail uses for the person when it prints no name. No name from anywhere else is added."
        attachments: "The files attached to the mail: name, and read. read true: the file's text was read when the card was made, so what the card says may come from it. read false: the file's content is unknown; its name is all there is, and nothing about what it holds may be assumed."
    }

    model_keys: {
        why: "These keys are a model's reading of the mail. Use them to understand it; for any name, number or date they state, rely on the printed keys."
        doc_type: "What kind of document the mail is."
        life_areas: "The areas of life the mail is about."
        title: "A short title of the mail."
        summary: "A short description of what the mail is."
        absent: "title and summary may be absent: code withholds both when they could name someone the mail does not print. Then read the mail from its subject and the printed keys."
    }

    mail_keys: {
        email_id: "The mail's id."
        from: "The sender's address; from_name is the sender's name as the mail shows it."
        subject: "The mail's subject line, as sent."
        date: "When the mail was sent, as a moment in the owner's timezone with its offset, on the same calendar as the Today line. Copy it, as given, into reported_date of every fact you take from this mail: reported_date is when the information reached the owner."
    }

    time_rule: "date says when the mail was sent; times say when the things in it happen or hold. Compare times with Today, never with date: a plan, an appointment, a deadline or a validity whose day is before Today is past, so the taxonomy's date rules apply (history or noise). Decide expiry from times and the content, as the kinds table says."

    about: "Whose matter the mail is, derived by code: owner (the owner's own), household (the owner together with people of his life, or several of them), person (one person of his life, who is in people, not the owner himself), unknown (the card could not tell). Write the fact about the person it concerns: a person's matter is a fact about that person in the owner's life, never about the owner. With unknown, decide from the content whose it is, if anyone's."

    yield: "Each mail yields zero or more candidate facts: none when it records nothing the owner would want his assistant to know later; one or several when it records separate things that would each be asked about on their own. Every candidate fact then goes through steps 2 to 7 as usual."
}
