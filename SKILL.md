---
name: roil-drawing
description: Use when the task is to generate images, optimize drawing prompts, or edit images with Nano Banana Studio from any workspace. This skill covers 绘图, 出图, 文生图, 改图, 参考图重绘, and prompt optimization, and it should use the Nano Banana Studio CLI (`./nbs` or the repo at `/Users/apple/Documents/GitHub/nano-banana-studio`) instead of reimplementing HTTP calls or importing backend internals directly.
---

# Roil Drawing

当任务是用 Nano Banana Studio 做绘图时，优先走 CLI，不要重复拼 HTTP 请求，也不要绕过 CLI 直接复制底层业务逻辑。

这个 skill 的执行层是 Nano Banana Studio CLI；参考库只用于构思和组织提示词。不要为了使用参考库而绕开 `./nbs`。

## Workspace resolution

1. 如果当前工作区根目录有 `./nbs`，直接在当前目录执行。
2. 如果当前工作区没有 `./nbs`，默认切到 `/Users/apple/Documents/GitHub/nano-banana-studio` 执行。
3. 只有当这两个位置都不存在 `nbs` 时，才说明当前环境缺少可用入口。

## Reference loading policy

按需加载参考，不要把所有参考一次性读进上下文：

1. 先读 `references/gallery.md`，判断任务属于哪类图。
2. 只读相关分类条目；普通任务最多选 1 个分类，混合风格最多选 2-3 个。
3. 需要润色提示词、中文文字、信息图、科研图、UI mockup、构图一致性时，再读 `references/craft.md`。
4. 用户明确要求“保留原词”“不要润色”时，不读 craft，不优化 prompt。

当前参考文件：

- `references/gallery.md`：分类路由和模板索引。
- `references/craft.md`：通用提示词结构、质量检查和失败修正。
- `references/patterns.md`：可直接改写的分类模板。

## Quick workflow

1. 先查看当前可用图片模型：

```bash
./nbs models --service image --json
```

2. 如果用户给的是一句较短需求，或明确希望你帮忙润色提示词，可以先优化 prompt；但这一步失败时不要中止出图，直接回退原始提示词继续：

```bash
./nbs prompt optimize \
  --prompt "生成一张关于具身智能的教学插图" \
  --subject textbook \
  --image-model gemini-3.1-flash-image-preview \
  --json
```

3. 文生图优先显式传 `--output` 和 `--json`。未显式指定模型时，让 CLI/后端自动回退所有已启用图片模型；指定模型时，也允许后端继续尝试其他供应商：

```bash
./nbs image generate \
  --prompt "生成一张关于具身智能的教学插图" \
  --subject textbook \
  --output /tmp/roil_drawing_image.png \
  --json
```

4. 如果希望一步完成“优化提示词 + 出图”，优先直接用 `--optimize`。即使优化模型失败，也要继续尝试生成图片：

```bash
./nbs image generate \
  --prompt "生成一张关于具身智能的教学插图" \
  --subject textbook \
  --optimize \
  --output /tmp/roil_drawing_image.png \
  --json
```

5. 改图或参考图重绘用 `image edit`，图片路径尽量传绝对路径：

```bash
./nbs image edit \
  --prompt "把图中的狗换成猫，保留原本的构图和光线" \
  --image /absolute/path/input.png \
  --model gemini-3.1-flash-image-preview \
  --output /tmp/roil_drawing_edit.png \
  --json
```

## Prompt drafting workflow

当用户只给一个短需求，而不是完整提示词时：

1. 选定图像类型：教学插图、科研图、信息图、海报、UI mockup、角色设定、产品图、摄影、国风水墨、像素/游戏等。
2. 从 `references/gallery.md` 找到分类，再从 `references/patterns.md` 取一个最接近的骨架。
3. 用 `references/craft.md` 检查：主体是否明确、构图是否可执行、文字是否逐字给出、输出比例是否合理、哪些内容必须保持不变。
4. 如果用户希望优化，优先用 `./nbs prompt optimize ... --json`；如果优化失败，使用你整理后的提示词继续生成。

不要把“平台/厂商”写进用户提示词，除非用户明确要求某种品牌或模型风格。

## Quality and size guidance

Nano Banana Studio 的 CLI 目前主要通过模型、用途和后端路由控制成本；如果 CLI 支持质量/比例参数，按下面原则选择：

- 草稿、多变体、探索：低成本模型或默认路由，不强制高质量。
- 教学插图、普通配图：默认路由即可，优先清晰构图和少量文字。
- 海报、中文文字、科研图、信息图、UI mockup：优先更高质量模型；提示词中明确文字、层级、布局和留白。
- 横向封面 / 宽屏场景：优先 `16:9` 或 landscape。
- 手机海报 / 竖版封面：优先 portrait。
- 图标 / 单物体 / 社交头像：优先 square。

## Working rules

- 默认优先使用已有登录态和后端认证链路。
- 只有在任务明确要求，或需要排查底层兼容性问题时，才传 `--api-key`、`--base-url` 或设置 `NBS_FORCE_DIRECT=1`。
- 需要结构化结果时始终加 `--json`，并在答复里带上 `output_path`、`model`、`requested_model`、`attempted_models`、`fallback_used`、`via`；如果命令返回了 `optimized_prompt` 或 `optimize_error`，也一并说明。
- 生成类命令优先显式传 `--output`，避免文件落点不明确。
- 若已经单独跑过 `prompt optimize`，后续 `image generate` 直接使用优化后的 prompt，不要重复再加 `--optimize`。
- 出图失败时，优先相信 CLI/后端内建的“全模型回退”链路；只在所有已启用 image 模型都失败后，再下结论说当前技能无法出图。
- `image edit` 当前主要走直连模式；如果失败，先区分是参数问题、模型不可用、鉴权问题，还是上游网关兼容问题。
- 仅在需要底层调试信息时临时加 `NBS_VERBOSE=1`，正常流程保持默认静音输出。
- `fallback_used: false` 表示没有发生模型回退，不是失败；失败以命令非 0 或 `success: false` 为准。

## Selection guidance

- 用户没有指定模型时，先跑 `./nbs models --service image --json` 看当前启用模型；执行生成时优先不传 `--model`，让 CLI/后端自动尝试所有已启用供应商。
- 用户强调“保留原词”或“不要润色”时，跳过 `prompt optimize`。
- 用户要多个变体时，运行多次 `image generate` 并为 `--output` 使用不同文件名。
- 如果需要可追踪结果，读取命令返回的 JSON；CLI 也会在输出图片旁边写一个同名 `.json` sidecar。
- 如果任务类型已经命中参考库，最终答复可以简短说明使用了哪个提示词分类，但不要把内部长模板全文贴给用户，除非用户要求。

## Failure triage

命令失败时，按下面顺序判断：

1. CLI 参数是否缺失或拼错。
2. `./nbs models --service image --json` 中是否真的有目标模型。
3. 当前是否已有后端登录态，或是否误触发了直连模式。
4. 是否属于上游平台鉴权、分组、网关兼容性问题。
5. 如果是文字错误、构图混乱或图表不可读，优先回到 `references/craft.md` 修提示词，而不是马上怀疑模型。

只有在 CLI 无法覆盖需求时，才继续下沉到 `backend/core/*`。
