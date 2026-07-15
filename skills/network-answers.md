# Let people ask your agent about your network

You built a private map of your LinkedIn network. This lets your agent answer
high-level questions about it for people in channels you share with it, without
handing over your contact list. Someone asks "does she know anyone in AI
enablement," and your agent can say "yes, about five people" or surface a few
real profiles with links, so the asker can reach out themselves.

It reads the same local database the map is built from, so every answer is
grounded in your actual connections, not a guess.

**Before you use this:**
- Run `agent-answers-you-in-channels.md` first, so your agent answers you in
  shared channels at all. This builds on that.
- The network observatory has to be set up (`data/linkedin.db` exists from
  `linkedin-import`), on the same machine as your agent.

Paste the block below **into your backchannel** (agents only take rule changes
there). It tells your agent what it may say about your network, how to look it
up, and where the line is.

---

```
Add this to your standing instructions, then read it back to me.

ANSWERING QUESTIONS ABOUT MY NETWORK
People in channels I'm in with you may ask about who I know. You can help, at a
high level, from my network database (the LinkedIn graph behind my Observatory).
Look people up with:

    python3 scripts/trellis.py recall "<what they're asking about>"

That searches names, companies, and job titles and returns matches with their
LinkedIn profile links.

WHAT YOU MAY ANSWER
- A count, when that's all they need: "Does she know anyone in AI enablement?"
  -> "Yes, about five people working in that area."
- A short list of suggestions, when someone's looking for a background, field,
  or role: reply with each person's name, one line on what they do, and their
  LinkedIn link, so the asker can reach out themselves.
- Keep it to professional fit: field, role, company, seniority.

WHAT YOU DON'T DO WITHOUT ME
- Anything that feels sensitive or personal: a contact detail beyond someone's
  public LinkedIn link, private context about a person, how or why I know them,
  or a request to introduce someone. Don't answer those in the channel. Message
  me in our backchannel with who's asking and what they want, and wait for my
  yes. I should only ever have to answer "yes," never retype the request.
- Never dump my whole connection list, never share emails, and never make an
  introduction on your own. You surface people and links; I decide who gets
  connected.

HOW TO ANSWER WELL
- Only when someone is actually asking about my network, and only in channels
  I'm already in with you.
- Ground every answer in what the lookup returns. If it finds nothing, say so
  plainly. Don't guess, and don't pad the list to look fuller.
```

---

## Good to know

- **It reads only the local database.** The agent runs `trellis.py recall` over
  `data/linkedin.db` on your machine. Nothing about your connections leaves that
  machine except the specific names and public LinkedIn links your agent chooses
  to surface in reply to a question.
- **Function and field are inferred from job titles.** There's no literal "AI
  enablement" field; the lookup matches title and company keywords plus the
  inferred function buckets (the closest is "Data & ML"). So treat field
  answers as "people whose titles point that way," not a verified list.
- **The backchannel gate is the whole safety story.** Public-fit questions get
  answered in the room; anything that edges toward private detail or an intro
  comes to you first. If you're ever unsure which side a question is on, err
  toward asking you.

## Why it's shaped this way

Your network is useful to people you trust, but the people *in* it didn't sign
up to be handed out. This keeps your agent on the useful side of that line: it
points at public profiles and lets the asker take it from there, and it routes
anything more personal back to you. You stay the one who decides who actually
gets connected.
