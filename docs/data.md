# 来源资料约定

资料是一个独立 GTFS 静态发布。一个压缩包就是一次发布的边界；包内各表的引用只在包内解释，ID 不是跨发布、跨日期的全局业务身份。

* calendar_dates 提供 holiday 服务的明确加开日期；没有 calendar.txt 不算引用丢失。
* frequencies 的 exact_times=0 表示按频率运行，行本身是频率规则，不是逐班精确时刻。
* stop_times 中的 25:10:00 仍在声明的原服务日内（时间可超过 24:00:00）。
* publish-2 的 stop_times 包含来源中缺失的站台引用（`missing-platform`），接收时应被整体拒绝并指向 `stop_times.txt:2`，不能从 publish-1 借行补齐。
* 压缩包不包含外部下载依赖。

## 发布内校验

接收时在单个发布内校验：

| 检查 | 拒绝方式 |
| --- | --- |
| 必需文件 agency / stops / routes / trips / stop_times | 缺失即整体拒绝 |
| calendar.txt 与 calendar_dates.txt 至少一份 | 缺失即整体拒绝 |
| 必需列、重复列名、行字段多于表头 | 指向文件与行 |
| stops.stop_id 唯一、parent_station 在包内且不指向自身 | 指向文件与行 |
| routes.agency_id、trips.route_id / service_id、stop_times.trip_id / stop_id、frequencies.trip_id 可解析 | 指向文件与行 |
| stop_sequence 按行程递增且不重复 | 指向文件与行 |
| 时间格式与 departure >= arrival、频率 end_time > start_time | 指向文件与行 |
| calendar_dates 同一服务同一日期不重复 | 指向文件与行 |

可选文件（例如没有 calendar.txt）不报引用丢失；空字段保留并显式标注，不补默认值。
