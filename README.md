# 公交静态发布包的服务日与引用闭包资料工具

这个 Python 项目把每个 GTFS 静态压缩包当作**一次发布**接收，并按服务日回答“当时发布了什么”。`schedule_bundles/core.py` 保留原有的单份结构预览，`archive.py` 按内容寻址保存原始字节，`publication.py` 负责接收与发布内校验，`query.py` 提供按服务日的查询，`change.py` 比较两次发布换掉的引用关系。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`；空清单表示只使用标准库。安装只在环境准备阶段执行：

```sh
python3 -m pip install -r requirements.txt
```

## 接收一次发布

```sh
python3 -m schedule_bundles check fixtures/publish-1.zip     # 只看候选资料，不写库
python3 -m schedule_bundles receive fixtures/publish-1.zip   # 校验通过后登记为当前发布
python3 -m schedule_bundles receive fixtures/publish-2.zip --no-publish  # 收下但不是当前版本
python3 -m schedule_bundles current
python3 -m schedule_bundles list
```

接收是先归档原始字节、再完整校验、最后才更新“当前发布”指针。候选发布有任何问题都会被整体拒绝，错误定位到原始文件和行；旧发布始终可用，不会出现挑几张能解析的表先盖掉旧版的中间状态。用 `check` 可以只检查候选资料，不改变发布库。

发布库布局（默认 `.work/publications/`）：

```
objects/<sha256>      原始压缩包内容，文件名不参与内容身份
tables/<sha256>.json  该发布解析后的表，仅供本发布内部查询
publications/<sha256>.json  成员清单、行数、原始文件 SHA-256 与校验事实
current.json          当前发布指针（最后一次通过校验的发布）
```

## 引用只在发布内解释

stops、routes、trips、stop_times、calendar、calendar_dates、frequencies 之间的引用只在**同一发布**内成立。ID 只是该发布内的成员标识，不是跨发布、跨日期的全局业务身份。

* 缺少可选文件（例如没有 calendar.txt）不等于引用丢失；只要 calendar_dates.txt 声明了服务，引用就能解释。
* 新完整发布撤掉线路，只改变这次发布的成员清单，不抹掉以前发布的资料。
* 坏引用会指出原始文件与行，例如 `stop_times.txt:2`，不会从别的日期或别的包借行补齐。
* 空字段原样保留，并明确标注为空；不会在导入时补 0 或补默认值。
* 多余字段、重复列名、重复主键不会被悄悄丢弃或覆盖。

## 按服务日查询

```sh
python3 -m schedule_bundles services                    # 发布声明了哪些服务日历
python3 -m schedule_bundles service-day 20261113        # 该服务日实际生效的行程与依据
python3 -m schedule_bundles trip t1                     # 某个班次的原始 stop_times 与频率规则
python3 -m schedule_bundles stop platform               # 站点的原始字段与 parent_station 层级
python3 -m schedule_bundles service-day 20261113 --publication <发布ID>
```

服务日口径：

* calendar.txt 与 calendar_dates.txt 共同生效，例外日期优先：`exception_type=1` 加开、`exception_type=2` 停开。
* 时间按自服务日 00:00 起的秒数解释；`25:10:00` 仍属于原服务日，并标注 `arrival_time_next_day`。
* frequencies.txt 的行保留为频率规则（`kind: frequency_rule`），不会展开成精确的逐班时刻。
* 上下车约束（pickup_type / drop_off_type）与 parent_station 原样返回；父站在本发布内解析不了时明确标注 `parent_resolved: false`。

## 比较两次发布

```sh
python3 -m schedule_bundles compare <发布ID或压缩包> <发布ID或压缩包>
```

比较两份发布各自写出的资料，报告：文件级变化、表级增删、逐记录的字段变化，以及**引用关系的增删**（每条都带原始文件和行）。相同名字的 ID 不会被当成同一个业务实体。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q schedule_bundles tests
```

## 本地演示

```sh
python3 -m schedule_bundles demo
```

演示实际读取来源资料，打印原始归档摘要及结构预览，使用临时目录并自动清理。所有示例身份与业务数值均为虚构，不需要外部账号。
