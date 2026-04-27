# roil-drawing

Codex skill for generating, editing, and redrawing images with Roil Drawing.

Roil Drawing is the standalone distribution target. Historical project CLIs are
not required by this skill.

The default production path is Roil platform authentication: use the signed-in
Roil session and let the platform handle generation, editing, prompt
optimization, and image/quota counting. Direct OpenAI API keys are only a
fallback for explicitly configured direct runtimes.

## Contents

- `SKILL.md` - skill instructions and workflow.
- `references/gallery.md` - image task routing index.
- `references/patterns.md` - reusable prompt pattern skeletons.
- `references/craft.md` - prompt quality and repair guidance.
- `agents/openai.yaml` - optional agent metadata.

## Usage

Install or copy this directory into your Codex skills folder, then invoke
`roil-drawing` for drawing, image editing, prompt optimization, or reference
image redraw tasks through Roil-native drawing capability.
