# 02: 批量 manifest + 单海报全链路

**What to build:** 提供最高层批量入口，扫描源目录、按图片内容建立 manifest、维护海报状态，并让一张海报从输入走到结构化 JSON；支持 dry-run、重复图片去重、坏图隔离和单张失败不影响其他海报。

**Blocked by:** 01（提取核心 prefactor）

**Status:** done

- [x] 批量入口能扫描目录并写入 SQLite manifest，记录图片哈希、路径、站点、方向、大小和状态
- [x] 三张 fixture 海报可以完整处理，每张写出一份结构化 JSON，状态变为 done
- [x] 内容相同的图片只处理一次，重复项在 manifest 中可识别
- [x] 再次运行只跳过已完成项，不重复识别
- [x] 损坏图片标记为 failed 并记录原因，其他海报继续完成
- [x] dry-run 只生成 manifest 和统计，不调用 OCR
