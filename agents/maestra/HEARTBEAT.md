---
last_updated: 2026-09-03T20:15:00-05:00
---
This heartbeat runs at 9:00 AM America/Chicago every Monday, Wednesday, and Friday. Its purpose is to assign Matt one useful Spanish homework assignment on Ouro.

## Destination

Publish in the `learn-spanish` team:

- Organization: `all` (`00000000-0000-0000-0000-000000000000`)
- Team: `learn-spanish` (`01a069eb-22fa-7221-a711-f172f1325116`)
- Student: `@mmoderwell`

Do not publish the assignment in the global catch-all team.

## Before Creating Anything

1. Inspect your recent posts and quests in `learn-spanish`.
2. Check Matt's comments, submissions, and feedback on recent assignments.
3. Recall his current level, interests, recurring errors, vocabulary due for review, and workload from memory.
4. Confirm that you have not already published an assignment for this scheduled date. If one exists, do not duplicate it.
5. Choose a different skill and format from the previous assignment when practical.

If Matt has not completed a difficult prior assignment, still keep the cadence but make today's work short and complementary rather than adding another large task.

## Weekly Rhythm

Use this as a default rhythm, adapting it to Matt's demonstrated needs:

- **Monday — input and noticing:** a poem, article, short video, or audio selection with a focused comprehension or language-noticing task.
- **Wednesday — interaction and retrieval:** a short post that asks Matt to respond in Spanish in the comments, reuse recent language, or discuss a topic.
- **Friday — production and synthesis:** a small quest for a written submission, structured reflection, or creative response.

The pattern is guidance, not a rigid template. Vary it when another assignment would teach better.

## Resource Selection

Curate material Matt is likely to enjoy:

1. Travel and the Spanish-speaking world
2. Artificial intelligence and technology
3. Science, nature, and discovery
4. Spanish-language poetry

For external material, delegate current web research to the search or research subagent. Choose a specific resource, not a search-results page. Open and verify the source before publishing. For YouTube, confirm that the video is available, primarily in Spanish, and appropriate in length and difficulty. Prefer original creators and reputable channels. For poetry, respect copyright: link to an authorized source, and only reproduce the full poem when it is clearly public domain or licensed for redistribution.

Do not recommend material merely because its topic fits. Identify the exact linguistic value: useful vocabulary, listening speed, accent exposure, a grammar pattern, rhetorical style, or cultural context.

## Assignment Requirements

Create exactly one assignment per heartbeat. Keep normal assignments to roughly 15–35 minutes.

Every assignment must clearly state:

- the learning objective;
- the specific material, with a working direct link when external;
- what Matt should read, watch, or listen to;
- what he should produce in Spanish;
- where he should respond;
- an estimated completion time;
- a reasonable due date, normally before the next scheduled assignment.

Write primarily in level-appropriate Spanish, using brief English support only where needed.

Use:

- an **Ouro post** when Matt should read/watch and answer in that post's comments;
- an **Ouro quest** when Matt should submit a substantial composition or another trackable artifact.

For a quest, create a closable quest with one clear item, publish it as open, use the exact contributor-key requirements returned by the quest item, and make the expected submission format explicit. Do not create multi-item curricula in one heartbeat.

Mention `@mmoderwell` in the published assignment. Do not include private learning history, sensitive personal details, or a public catalogue of his past mistakes.

## After Publishing

Store only durable teaching context in memory: the resource and skill assigned, approximate difficulty, language introduced for future retrieval, and what evidence to inspect next time. Avoid storing a copy of the whole assignment.

Return:

```json
{
  "action": "assigned_homework",
  "details": "<post or quest title and link>",
  "worth_remembering": true,
  "memory_notes": [
    "<brief skill/resource/difficulty note>"
  ]
}
```

If today's assignment already exists, return:

```json
{"action": "none"}
```
