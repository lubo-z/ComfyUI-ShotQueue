# ComfyUI-ShotQueue — v0.1.0

轻量任务列表：只保存首帧路径、尾帧路径、prompt、duration。选择已有图片时按需显示单张预览；无第三方依赖，不持久缓存模型、latent、embedding 或预览图片。

也可使用独立的[文件夹队列脚本](script/README.md)，递归读取生产目录中的 `EXPORT` 文件夹并逐条提交任务，无需安装自定义节点或保持浏览器打开。

## 安装

把整个 `ComfyUI-ShotQueue` 文件夹复制到 `ComfyUI/custom_nodes/`，重启 ComfyUI，然后刷新浏览器。在节点菜单 `Shot Queue` 下添加 **Shot Queue**。

```text
ComfyUI/custom_nodes/ComfyUI-ShotQueue/
    __init__.py
    nodes.py
    web/shot_queue.js
```

## 操作

- **Add Shot / Remove / Move Up / Move Down**：管理任务。
- **Browse**：从浏览器所在电脑选择图片，上传到 ComfyUI 服务端 `input/shot_queue/`；保存返回的相对路径。每次上传使用唯一文件名，避免覆盖其它任务的图片。不会读取浏览器真实绝对路径，也不会生成预览。上传需要临时传输文件字节，但不会由本扩展解码为 tensor。
- **Select Image**：列出 ComfyUI 服务端 `input/` 内已有的图片（包括 `shot_queue/`）。选择文件后按需加载预览，点击 **Use Image** 才保存相对路径；**Cancel** 不修改任务，无需再次上传。
- 路径可手填，例如 `shot_queue/example.png`（相对于 ComfyUI 服务端的 `input/`）。内置 LoadImage 也支持带 `[output]` / `[temp]` 标注的对应目录路径，以及这些目录内的绝对路径；目录外的绝对路径不可用。移动 workflow 到另一台机器时，还需复制图片。
- **Select**：选择当前任务。节点自带 `shot_index` 数值从 **0** 开始；界面 Shot 编号从 **1** 开始。
- ComfyUI 普通 **Queue** 按钮仅执行当前选中的一个任务，不会自动前进。
- **Run Queue**：从第一条开始提交整个列表。未勾选清理时，依次把所有任务预先提交到 ComfyUI 队列；显示 `Queued ...` 后可以关闭浏览器，ComfyUI 会继续执行。已提交任务即使前一条运行失败也会继续执行；提交期间若关闭浏览器，剩余任务不会入队。
- **Clear cache + unload models between shots**：默认关闭。勾选后改为浏览器逐条调度：确认当前 workflow 成功后才提交下一条，运行期间须保持浏览器打开。每条非最后一条任务提交成功时预先请求 ComfyUI 的本地 `/free`；worker 在该任务结束后清除执行缓存并卸载模型。重复加载 H3 模型会降低速度。该请求作用于整个 ComfyUI 实例；运行期间不要从其它客户端同时提交任务。
- **Stop Queue**：仅在仍在提交时停止后续提交；不删除、不中断已经提交的任务。预先提交完成后，需使用 ComfyUI 自己的队列控件取消任务。节点被删除或浏览器刷新/关闭时也会停止尚未提交的任务。
- 执行和上传期间不接受列表编辑。Run Queue 不会更改手动选中的任务。一个执行图仅支持一个连接的 Shot Queue；同一浏览器页面同时仅允许一个本扩展的队列运行。
- 无清理模式会在 ComfyUI 内存队列中保存每条任务的 workflow 快照，大列表会增加 RAM 占用；ComfyUI 重启后未完成的队列不保证保留。本扩展不锁定其它页面、其它客户端或其它 ComfyUI 插件的提交。

## 接入原 workflow

本扩展采用 ComfyUI 的 `WEB_DIRECTORY`、`app.registerExtension`、DOM widget、标准 `shots_json` / `shot_index` 输入。任务 JSON 保存于标准 widget 的 `widgets_values`，清理选项保存在节点的 `properties`；普通 workflow 导入、导出、复制节点均通过这些值恢复。后端不额外写文件，也不保存执行游标。

可直接导入 `workflow/video_minimax_h3_i2v_shot_queue.json` 作为示例。它基于仓库中的 I2V workflow，已接好队列、两个当前图片加载节点、prompt、duration 和 SaveVideo 文件名。内置两条无图片示例任务；先检查 workflow 的模型文件是否已安装，再点击节点中的 **Run Queue**。`workflow/video_minimax_h3_i2v.json` 是使用普通 LoadImage 的可编辑版本，已设置独立脚本需要的 `QUEUE_*` 节点标题。

Shot Queue 的输出：

| 输出 | 类型 | 含义 |
|---|---|---|
| start_frame_path | STRING | 当前首帧路径 |
| end_frame_path | STRING | 当前尾帧路径，可空 |
| prompt | STRING | 当前 prompt |
| duration | FLOAT | 秒，支持小数 |
| shot_index | INT | 当前零基序号 |
| shot_name | STRING | 自动生成，例如 shot_001 |

对于 `workflow/video_minimax_h3_i2v.json`：添加一个 **Shot Queue** 和两个 **Shot Queue • Load Current Image**。将 `start_frame_path`、`end_frame_path` 分别接到两个加载节点的 `path`，再将它们的 IMAGE 输出接到 H3 子图的 `first_frame`、`last_frame`，替换原有两个 LoadImage 的连接。将 `prompt` 接到子图的 `prompt`，`duration` 接到子图的 `value_1`（秒数输入）。保留子图内部已有的秒数转帧数公式；不要直接把 duration 接到 `length`。如需按任务命名视频，可将 `shot_name` 接到 SaveVideo 的 `filename_prefix`。

加载节点只在执行时调用 ComfyUI 内置 LoadImage 解码当前文件，不在队列中加载所有图片。支持 IMAGE 和 MASK 输出，缓存失效检测沿用内置加载节点。路径为空时输出 `None`，可连接到该 H3 子图的可选首帧或尾帧输入，因此队列中可以混用有图和无图任务。

如果其它 workflow 的 IMAGE 输入是必填项，对应图片路径不能留空。duration 仅表示秒；如果其它 workflow 接收 INT 秒或帧数，需要按模型要求使用已有转换节点。本包不会猜测帧数公式或模型支持时长。

Run Queue 执行当前图中的所有可执行输出，所以建议图中只保留目标视频工作流。输出视频命名由原 SaveVideo 节点决定；可按其接口接入 shot_name。

## LoRA

两个画布 workflow 的 H3 节点均提供 `enable_turbo_lora` 开关，开启时使用所选 turbo LoRA 和 `turbo_steps`，关闭时使用基础模型和基础步数。默认开启，turbo 强度为 1，步数为 8。

额外 LoRA 不再预置。需要时进入 H3 子图，在 `If/Else Switch (Model)` 后手动串联 `LoraLoaderModelOnly`，将最后一个加载节点的 MODEL 输出接到 `BasicScheduler` 和 `BasicGuider`。这样额外 LoRA 在 turbo 开启或关闭时均生效；每个节点可独立选择文件和强度。

独立脚本支持 `--turbo` / `--no-turbo`，也可用 `--lora-dir` 选择 ComfyUI 服务端 `models/loras/` 下的额外 LoRA 文件夹；详见[脚本说明](script/README.md)。

## 当前边界

- 不实现失败重试、断点续跑、跨页面协调或服务器常驻调度。
- 默认保留 ComfyUI 的执行缓存和已加载模型；勾选清理选项时使用 ComfyUI 的全局清理机制，仍不承诺 RAM/VRAM 归零。
- 后端只验证选中任务的四个字段、正数有限时长和合法索引；不提前检查其它任务图片，不加载文件。路径有效性由执行时的加载节点检查。
- 已通过纯 Python 后端检查与 Node.js mock 前端行为检查。尚未在真实 ComfyUI / 官方 MiniMax H3 workflow 上验证 UI 布局、上传和视频生成；属于第一版可安装开发包。不同前端版本的 DOM widget/序列化接口可能需适配。

## 开发检查

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s script -v
node tests/test_frontend.cjs
```

官方扩展文档：https://docs.comfy.org/custom-nodes/js/javascript_overview
