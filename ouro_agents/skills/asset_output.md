---
name: asset-output
description: Standard pattern for saving output as Ouro assets and reporting results
load: stub
---

## Saving Output as Ouro Assets

When your work produces reusable content, save it as an Ouro asset:

1. Use `create_post` (or `create_dataset` / `create_file` as appropriate) with the
   `org_id`, `team_id`, and `visibility` from the Platform context and Ouro asset
   placement sections.

2. Call `final_answer` with a JSON object containing the asset metadata:
   ```json
   {
     "asset_id": "<id from create call>",
     "asset_type": "comment"|"post"|"dataset"|"file"|"route"|"service"|"conversation"|"quest",
     "name": "<asset name>",
     "description": "<one-line summary>",
     "content": "<full output text>"
   }
   ```

3. If the create tool is unavailable, return the full content directly in
   `final_answer` as plain text.

4. If no asset was created (e.g. the task was informational), keep `final_answer`
   brief — report what you did and key results.
