# ComfyUI-ShotQueue — v0.1.0

轻量任务列表：只保存首帧路径、尾帧路径、prompt、duration。选择已有图片时按需显示单张预览；无第三方依赖，不持久缓存模型、latent、embedding 或预览图片。

可使用[workflow 生成脚本](script/README.md)，递归读取生产目录中的 `EXPORT` 文件夹： `python3 script/build_shot_workflow.py --jobs "/path/to/production" --out "production_review.json"`。脚本将所有任务填入模板的 Shot Queue，并复制首尾帧到 ComfyUI 的 `input/`；随后载入生成的 workflow，预览、修改并点击 **Run Queue**。无需 API 连接，详见[脚本说明](script/README.md)。

目录结构和文件名可通过 `--config /path/to/shot_layout.json` 自定义，也可编辑脚本顶部的 `DEFAULT_LAYOUT`。示例配置见 [shot_layout.example.json](script/shot_layout.example.json)。生成时保留模板中的二采、模型和 LoRA 设置。

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
- **Preview**：点击首帧或尾帧路径左侧的按钮，在该路径下方预览当前图片；点击 **Close Preview** 关闭。不修改任务路径，关闭时释放预览图片。
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

可直接导入 `workflow/video_minimax_h3_i2v_shot_queue.json` 作为示例。它基于仓库中的 I2V workflow，已接好队列、两个当前图片加载节点、prompt、duration 和 SaveVideo 文件名。模板中的 Shot Queue 初始为空白任务；先填入图片、prompt 和 duration，并检查模型文件是否已安装，再点击 **Run Queue**。

Shot Queue 的输出：

| 输出 | 类型 | 含义 |
|---|---|---|
| start_frame_path | STRING | 当前首帧路径 |
| end_frame_path | STRING | 当前尾帧路径，可空 |
| prompt | STRING | 当前 prompt |
| duration | FLOAT | 秒，支持小数 |
| shot_index | INT | 当前零基序号 |
| shot_name | STRING | 自动生成，例如 shot_001 |

接入其它 MiniMax H3 I2V workflow 时，添加一个 **Shot Queue** 和两个 **Shot Queue • Load Current Image**。将 `start_frame_path`、`end_frame_path` 分别接到两个加载节点的 `path`，再将它们的 IMAGE 输出接到 H3 子图的 `first_frame`、`last_frame`，替换原有两个 LoadImage 的连接。将 `prompt` 接到子图的 `prompt`，`duration` 接到子图的 `value_1`（秒数输入）。保留子图内部已有的秒数转帧数公式；不要直接把 duration 接到 `length`。如需按任务命名视频，可将 `shot_name` 接到 SaveVideo 的 `filename_prefix`。

加载节点只在执行时调用 ComfyUI 内置 LoadImage 解码当前文件，不在队列中加载所有图片。支持 IMAGE 和 MASK 输出，缓存失效检测沿用内置加载节点。路径为空时输出 `None`，可连接到该 H3 子图的可选首帧或尾帧输入，因此队列中可以混用有图和无图任务。

如果其它 workflow 的 IMAGE 输入是必填项，对应图片路径不能留空。duration 仅表示秒；如果其它 workflow 接收 INT 秒或帧数，需要按模型要求使用已有转换节点。本包不会猜测帧数公式或模型支持时长。

Run Queue 执行当前图中的所有可执行输出，所以建议图中只保留目标视频工作流。输出视频命名由原 SaveVideo 节点决定；可按其接口接入 shot_name。

## 二采模板

当前 `workflow/video_minimax_h3_i2v_shot_queue.json` 已接入 [LBH Latent Upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler) 二采。尺寸选择器指定最终输出尺寸，一采自动使用约一半宽高（按 32 对齐）。一采的音视频 latent 分离后，仅视频进入 3D 放大节点，再与音频合并交给 **MMH3 Split Upscale** 分块精修；二采使用目标尺寸重新生成的首尾帧 conditioning，最终输出保留一采音频。

需要已安装的 LBH 节点和 `models/latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors`。子图内的试用设置为：二采 `normal`、2 步、`denoise=0.35`（子图内「二采 · 调度器」设置）；时间分块 73 帧、重叠 22 帧；空间分块 512×512、重叠 25%。放大节点开启时间分块和执行后卸载。请先用单条 shot 检查画质、接缝和显存；这些设置尚未经过实际生成验证。原一采模板备份为 `workflow/video_minimax_h3_i2v_shot_queue.single_pass.backup.json`。

如果二采报 `hostbuf_file_reader_read failed`，且前面的日志出现 `cuMemAllocHost ... out of memory`，表示动态模型加载申请锁页主机内存失败。先停止 ComfyUI，在原启动命令中加入 `--disable-dynamic-vram --disable-pinned-memory` 后重启，再试单条 shot。这会切换到常规模型加载并禁用锁页内存，可能降低速度；仍需足够的 RAM/VRAM。此方法已核对本地 ComfyUI 参数和加载路径，尚未完成实际生成验证。

## LoRA

当前模板及一采备份的 H3 节点均提供 `enable_turbo_lora` 开关，开启时使用所选 turbo LoRA 和 `turbo_steps`，关闭时使用基础模型和基础步数。默认开启，turbo 强度为 1，步数为 8。

当前模板保留了 `Style LoRA (MiniMax H3)` 加载节点及其 `retro-toon-70s.safetensors` 设置；使用前按本机模型修改文件名和强度。需要添加其它 LoRA 时，在模型开关后串联 `LoraLoaderModelOnly`，将最后一个 MODEL 输出接到一采的调度器和 guider，以及二采的调度器和分块精修节点，使两次采样使用同一模型。

## 当前边界

- 不实现失败重试、断点续跑、跨页面协调或服务器常驻调度。
- 默认保留 ComfyUI 的执行缓存和已加载模型；勾选清理选项时使用 ComfyUI 的全局清理机制，仍不承诺 RAM/VRAM 归零。
- 后端只验证选中任务的四个字段、正数有限时长和合法索引；不提前检查其它任务图片，不加载文件。路径有效性由执行时的加载节点检查。
- 已通过纯 Python 后端检查与 Node.js mock 前端行为检查。尚未在真实 ComfyUI / 官方 MiniMax H3 workflow 上验证 UI 布局、上传和视频生成；属于第一版可安装开发包。不同前端版本的 DOM widget/序列化接口可能需适配。

## 开发检查

```bash
python3 -m unittest discover -s tests -v
node tests/test_frontend.cjs
```

官方扩展文档：https://docs.comfy.org/custom-nodes/js/javascript_overview
