---
name: research-generator
description: Research a topic and produce a client-ready deliverable pair — a self-contained HTML slide deck (walkthrough, with charts, comparison matrices, and images) and a 3-5 page Word document (full write-up with cited sources) — covering role, topic, purpose, audience, background, implementation resources, cost/time, problem solved, alternatives, and a recommendation. Use whenever the user asks to "research X for a client," build a one-pager plus slides on a topic, prepare a discovery/recommendation deliverable, or wants both a presentation and a written report on the same subject. Trigger even if they only ask for one of the two outputs by name (e.g. "make me slides on X") if the surrounding context is a client/consulting engagement.
---

# Research Generator

## Purpose

Produce a matched pair of client deliverables from a single research pass on
one topic: a short HTML slide deck for walking a client through the material
live, and a 3-5 page Word document they can leave behind to read on their
own. Both outputs share the same nine-part structure and the same underlying
research — the deck compresses it, the doc expands it. Consistency between
the two matters more than either one being clever in isolation: a client
should never notice the deck and the doc disagree.

A good deliverable in this format also earns attention rather than just
informing — real visuals, an honest comparison matrix, and the occasional
well-placed fun fact make a client actually engage with the material instead
of skimming it. None of that is worth doing, though, if it costs credibility:
every chart, table, and fun fact must be traceable to where it came from. See
Step 4 for how to do both at once.

## When to Use

Use this skill any time someone wants a topic turned into a consulting-style
recommendation package — evaluating a tool, a vendor, a technical approach, a
new service line, a compliance requirement, anything where the deliverable is
"here's what this is, what it costs, and what we recommend." Trigger it even
if the request only names one output ("just make slides on X") when the
context is client-facing — offer the paired doc rather than assuming the
other output is unwanted.

Don't use it for open-ended research with no client/recommendation framing
(pure literature review, personal curiosity) — see [deep-research] for that
kind of report instead.

## Step 1: Gather the Four Framing Inputs

Before researching, you need four things. If the user's request already
supplies them, extract and confirm in one line rather than re-asking. If any
are missing, ask — these four inputs change what "good" looks like for every
downstream section, so guessing wastes the research pass:

1. **Role** — whose voice/perspective is this written from? (e.g. "IT
   consultant," "security advisor," "solutions architect at Helix Digital
   Partners") This sets the vocabulary and authority level of the writing.
2. **Topic** — the specific thing being researched. Push for specificity: "a
   patient scheduling tool" is harder to research usefully than "AI-assisted
   appointment scheduling for a multi-clinic community health network."
3. **Purpose** — what decision or action should this deliverable enable? (e.g.
   "help the client decide whether to adopt X," "justify a budget request,"
   "compare build vs. buy")
4. **Audience** — who is actually reading/watching this? A CFO, a clinical
   director, and a dev team lead need different levels of technical depth and
   different emphasis in the cost/ROI framing.

Ask for whichever of these four are missing in a single batch of questions
rather than one at a time — the user shouldn't have to answer a multi-turn
interview to get started.

## Step 2: Research the Topic

Research using whatever web-search capability is available in this session.
For each of the nine content sections below, gather enough real, current
information to write from — don't fabricate vendor names, prices, or
statistics. Where a hard number (cost, timeline, adoption rate) can't be
verified, say so explicitly in the output ("estimated," "varies by vendor")
rather than inventing false precision. A client-facing deliverable that
states a wrong number confidently is worse than one that honestly ranges it.

While researching, also keep an eye out for the material that will make Step
4's visuals possible: numbers worth charting, a genuine point of comparison
across alternatives, and — if one turns up naturally — a real, verifiable
"fun fact" (a surprising adoption number, an origin story, a scale
comparison). Gather this alongside the core research; don't go looking for
it afterward, which is how invented "facts" creep in.

Record where each fact, number, and comparison came from as you go (source
name, publisher/organization, URL if available) — Step 4's citations and
Step 6's resource list both depend on this, and reconstructing it after the
fact is unreliable.

If the [deep-research] skill is available and the topic is complex enough to
warrant a multi-source, adversarially-checked report, consider invoking it
for the research pass and then shaping its findings into the nine-section
structure below — don't duplicate its fan-out/verify machinery by hand.

## Step 3: The Nine-Part Content Structure

Both outputs are built from the same nine parts, in this order. Research and
draft this content once; only the packaging differs between the deck and the
doc.

1. **Role** — one line stating the perspective/authority this deliverable
   speaks from.
2. **Topic** — what is being evaluated, defined precisely enough that a
   reader unfamiliar with it understands the scope.
3. **Purpose** — why this deliverable exists and what decision it supports.
4. **Audience** — who it's written for and what they care about.
5. **Background** — what the reader needs to know to follow the rest: how the
   space/technology/approach works, current landscape, and — importantly —
   the concrete resources needed to implement it (tools, integrations,
   staffing, vendor dependencies, technical prerequisites). A fun fact fits
   naturally here if one turned up in research.
6. **Cost and Time** — realistic implementation cost ranges and timeline,
   broken into phases if the work isn't a single lump (e.g. pilot vs. full
   rollout). Flag assumptions behind any estimate. Usually the section with
   the clearest chart (cost by phase/option, or a timeline).
7. **Problem Solved** — the specific pain point or gap this addresses, stated
   concretely (not "improves efficiency" but what specifically becomes
   possible or stops being broken). Also a natural home for a fun fact if the
   Background section doesn't already have one.
8. **Strongest Alternatives** — 2-4 genuine competing approaches or vendors,
   each with a fair one-line characterization of its tradeoff versus the main
   option. This is the section to build as a full comparison matrix (Step 4)
   rather than prose — a recommendation without honestly-presented
   alternatives reads as sales copy, not advice.
9. **Recommendation for the Client** — a direct, decidable recommendation
   tied back to the audience's priorities from part 4. Hedge only where the
   research genuinely doesn't support a clean answer, and say so rather than
   forcing false confidence.

## Step 4: Make It Engaging — Charts, Matrices, Images, and Fun Facts

Visuals and a bit of color are what make a client actually read the doc and
stay with you through the deck, instead of treating either as boilerplate.
Use this section's tools throughout Step 3's content — not as a bolt-on
"visuals slide" at the end.

**Rule that applies to everything below:** any chart, graph, or matrix table
must be captioned with (a) its source — where the underlying data came from
— and (b) the exact metric being shown, including units and time period. If
a plotted or tabulated value is calculated rather than directly reported
(e.g. 3-year TCO, cost per patient, % ROI), state the formula used to derive
it, either in the caption or a footnote. A chart nobody can trace back to a
number is worse than no chart — it just looks persuasive without being
verifiable.

- **Charts/graphs** — use them where a comparison is genuinely numeric: cost
  across options, a rollout timeline, adoption trends. In the HTML deck,
  build simple charts with inline SVG or CSS (bars, simple line paths) —
  there's no charting library available since the deck can't reach a CDN. In
  the Word doc, either mirror the same data as a table or embed a rendered
  chart image; either way the source/metric/formula caption travels with it.
- **Matrix comparison tables** — build the Strongest Alternatives section as
  an actual matrix: rows are each alternative (include the recommended
  option as a row too, not just competitors), columns are the criteria this
  audience cares about (cost, implementation time, integration effort,
  support/maturity, scalability, etc.). Use a consistent scale (✓/✗,
  Low/Med/High, $/$$/$$$, or real figures where they genuinely exist) — don't
  force false quantification onto a judgment call. Cite the source behind
  each column that rests on an external claim (vendor pricing page, review
  site, docs) in a footnote under the matrix. Build this matrix into both
  outputs — it's usually the single most persuasive artifact in the whole
  deliverable, so it earns the space in both.
- **Images** — prefer simple inline SVG icons/illustrations over hunting for
  real photos, since the deck has to stay self-contained with no external
  network calls at view time (a fetched image either gets embedded as a
  base64 data URI or it breaks). If a specific real image genuinely helps
  (a vendor screenshot, a publicly available architecture diagram) and can
  legitimately be embedded, encode it as base64 and caption it with source
  attribution — and keep it visually distinct from decorative icons so a
  reader can't mistake an illustration for evidence. Skip imagery that adds
  no informational value; one relevant image per slide is plenty; a slide of
  clip-art reads as unserious in a client deliverable.
- **Fun facts** — add one genuine, sourced fun fact where it actually
  supports Background or Problem Solved (an adoption statistic, an origin
  story, a scale comparison that makes an abstract number concrete). It
  still needs a source — "fun" doesn't waive the no-fabrication rule from
  Step 2. If nothing genuinely interesting turned up, skip it; a forced fun
  fact undermines the credibility of everything else in the deliverable. In
  the deck, set it apart visually (a callout/aside box) so it isn't mistaken
  for a core finding the client needs to act on.

## Step 5: Build the HTML Slide Deck

Produce one self-contained `.html` file — inline CSS, no external assets or
CDN links, since it needs to open standalone in a browser with no network
access. Structure it as one slide per content section above (nine slides),
plus a title slide at the front (topic, role/presenter, audience, date) —
ten slides total. Use `<section class="slide">` blocks with a simple
JS keyboard/click handler (arrow keys, click-to-advance) so it functions as
an actual walkthrough deck rather than a scrolling document — this is a
presentation aid, not a webpage.

Each slide should read as a live talking-point summary, not a copy-paste of
the doc's paragraphs: short headline, 3-6 bullet points max, and — per Step
4 — a chart, the comparison matrix, an image, or a fun-fact callout wherever
one earns its place on that slide. If a slide needs more than a scrollable
amount of text to make its point, the content belongs in the Word doc
instead — cut it down for the deck. Keep any source/metric caption small and
unobtrusive (a footer line) — full citation detail lives in the doc's
resource list, not repeated in full on every slide.

Keep styling professional and client-ready: a clean sans-serif, restrained
color palette, generous whitespace, consistent header treatment across
slides. The goal is engaging, not flashy — don't reach for animation or
cleverness that a client would need explained.

## Step 6: Build the Word Document

Use the [docx skill](docx) to produce the `.docx` file — follow its
conventions for headings, styling, and document structure rather than
hand-rolling formatting logic here. Target 3-5 pages: enough room to write
each of the nine sections as real paragraphs (not bullet fragments), but not
padded. If a topic naturally fills more or less, say so to the user rather
than artificially stretching or compressing to hit the page count.

Structure:

- Title page or header block: topic, prepared-for (audience), prepared-by
  (role), date.
- One heading per content section, in the Step 3 order.
- Where the deck uses a chart or the comparison matrix, mirror it in the doc
  as a proper table or embedded chart image, same source/metric/formula
  caption attached — the doc is the leave-behind reference, so it should
  stand alone without the deck.
- A closing recommendation section that's slightly more detailed than the
  deck's version, since the reader has time to absorb the reasoning, not just
  the conclusion.
- A final **Resources Used in Research** section listing every source relied
  on for a factual claim, number, chart, or matrix column — title,
  publisher/organization, and URL where available. Enough detail that the
  client or their team could go verify a claim themselves. This section is
  required in the doc even when the deck only shows short source footers.

## Step 7: Confirm Before Finishing

Before presenting the outputs as done, check that:

- The recommendation in the deck and the doc actually agree with each other.
- Every number stated as fact was found during research, not invented; ranges
  and "varies" are used honestly where precision isn't available.
- Every chart, graph, and matrix table has a source and metric caption, and
  any calculated figure states the formula used.
- The alternatives section names real, currently-relevant options, not
  strawmen, and is presented as the Step 4 comparison matrix.
- Any fun fact used is genuinely sourced, not invented for color.
- The doc's Resources Used in Research section is present, non-empty, and
  actually corresponds to the sources cited in the content, charts, and
  matrix.
- Both files open cleanly (the HTML in a browser, the docx in Word) before
  telling the user they're ready.

Tell the user where both files were saved and offer to adjust tone, depth, or
emphasis for either output — since the same research pass drives both, a
requested change (e.g. "audience is actually more technical") usually needs
to be applied to both outputs, not just the one they mentioned.
