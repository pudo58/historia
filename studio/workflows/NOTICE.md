# Workflow provenance

Adapted from Comfy-Org/workflow_templates commit
`fc54797eb70273aee6e3918eeeecc1d0ac0760e1`:

- templates/image_qwen_image.json (base non-distilled branch)
- templates/image_qwen_image_edit_2509.json (active Lightning branch)
- templates/video_wan2_2_14B_i2v.json (active Lightning branch)

Source: https://github.com/Comfy-Org/workflow_templates/tree/fc54797eb70273aee6e3918eeeecc1d0ac0760e1

Node input definitions checked against ComfyUI commit
`ee71d5c4993f29086b27fde1629a945ae48425bf`:
`nodes.py`, `comfy_extras/nodes_qwen.py`, `nodes_wan.py`, `nodes_video.py`.
Graph serialization and default prompts/resolution were adapted for this studio.
No claim of GPU validation: installation verification records per-host smoke evidence.
