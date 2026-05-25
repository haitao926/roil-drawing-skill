---
name: roil-drawing
description: Use when the task is to generate images, optimize drawing prompts, edit images, redraw from references, or create visual assets with Roil Drawing. This is the single teacher-facing drawing skill. It covers 绘图, 出图, 文生图, 改图, 参考图重绘, and prompt optimization, and should decide internally whether to use local Roil/NBS login, the public Roil platform, or other available drawing execution paths.
---

# Roil Drawing

Roil Drawing 的默认目标很简单：直接给图。不要先做长篇分析，不要先展示链路，不要先谈 key。

## Default Flow

1. 直接运行 `roil_draw.py`。
2. 成功就返回图片输出路径。
3. 如果返回 `needs_cli_sync`，让用户打开返回的授权链接；不要让用户安装 `nbs`。
4. 如果返回 `needs_platform_login`，只给登录链接：`https://image.roil.top/`
5. 只有用户明确要求排查时，才单独运行 `roil_preflight.py`

默认执行入口：

```bash
python3 .codex/skills/roil-drawing/scripts/roil_draw.py \
  --prompt "..." \
  --out output/roil-drawing/roil-drawing.png \
  --open-platform \
  --json
```

旧镜像环境使用：

```bash
python3 .agents/skills/roil-drawing/scripts/roil_draw.py --prompt "..." --json
```

## Interaction Rules

- 用户要图时，先生成，不先贴预检摘要。
- 不要先读 `~/.nbs/auth.json`、环境变量、`backend/` 或模型配置来“找入口”。
- 不要因为缺少根目录 `nbs` 文件就让用户安装 `nbs`；skill 已经能用 Roil 平台后端直接出图。
- 不要主动尝试 `OPENAI_API_KEY` 或其他 key fallback。
- 需要登录时，只说“请先登录”并给链接，不展开认证细节。
- 只有脚本失败且错误信息不足时，才继续诊断。

## Login Handoff

统一登录链接：

```text
https://image.roil.top/
```

统一登录提示：

```text
请先登录 Roil 平台：https://image.roil.top/
```

## Prompt References

- 先读 `references/gallery.md` 判断任务类型。
- 普通任务最多读 1 个分类。
- 信息图、科研图、中文文字、构图一致性任务，再读 `references/craft.md`
- 用户明确说“不要润色”时，不优化 prompt。

## Debug Only

只有在用户明确要排查登录态、平台可达性或 CLI 路由时，才运行：

```bash
python3 .codex/skills/roil-drawing/scripts/roil_preflight.py --json
```

执行层以 `scripts/roil_draw.py` 和 `scripts/roil_preflight.py` 为准。
