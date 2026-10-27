# Agent 入口

执行站牌 OCR、迁移运行环境、接入 OCR/VLM 服务或修改提取规则时，先阅读 [Agent 调用约定](docs/agent-guide.md)，按其中的输入检查、执行、结果验收步骤完成任务。

字段语义、横竖版支持边界和未修复问题统一记录在 [精简模式规则](docs/endpoints-mode.md)。接口参数以当前 `python -m buspipe run --help` 和源码为准；文档中的历史验收数据只代表指定样本。

需要把站点-线路-方位映射到外部线路导出表时，按 [导入方位映射规则](docs/direction-mapping.md) 执行：先按线路走向和整板证据判断，再用 XMP 图片时间加权；没有唯一证据时留空，不猜。

修改版面 px 阈值前先读 [模板族阈值修改规则](docs/template-profiles.md)：按模板族和宽高比例分派，保留旧模板分支，先用旧模板基准做逐项回归。
