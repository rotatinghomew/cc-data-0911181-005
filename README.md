# 新课与跨校合班下服务

报名计划服务维护招生实录、按已知时点截取的单课程末值预测及历史预测发布记录。

## 运行环境

使用 Python 3.11，仅依赖标准库，无外部服务、账号或网络数据。
示例数据使用虚构标识和本地合成记录，不涉及真实学员。

```bash
python3 -m app check
python3 -m app summary
python3 -m app serve --host 127.0.0.1 --port 8080
```

服务提供只读 JSON 查询：`/api/history`, `/api/forecast`, `/api/releases`,
`/api/methods`, `/api/reservation-basis`。
`/health` 显示存活及表计数，`/api/records?table=表名` 可查看已知数据表。
详细字段语义、计数单位和当前能力见 `docs/数据契约.md`。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests smoke.py
python3 smoke.py
```

测试从实际数据装载进入领域计算、报告与真实 HTTP 服务，并用临时副本验证错误输入。
冒烟程序动态分配本地端口，启动服务后读取全部业务查询并原样输出 JSON，随后正常关闭。
查询不写回数据，因此可以直接重复运行；验证不会访问项目外文件。

## 容器

```bash
docker build -t enrollment-outlook:local .
docker run --rm --network none enrollment-outlook:local
docker run --rm --network none enrollment-outlook:local python3 smoke.py
```

镜像只封装本应用，测试使用回环网络，不依赖宿主端口。
无第三方包依赖，`requirements.lock` 记录此状态。

## 需求预测

`/api/forecast` 按时点截断（`as_of`）只用当时已知的招生与课程计划，输出课程、
校区、方向和全县四层需求区间与超容量风险。满班观测按右删失处理，只作为需求下界；
区间端点来自各层自己的聚合分布，因此可加的是人数期望而不是区间端点。合办课程
在课程层和全县层各出现一次，在校区层按份额划分，不会重复计入两个校区。新课借用
历史证据时会逐条列出借用的课程、关系与时点，从不使用它后来实际招到的人。

`/api/methods` 在连续招生期上比较候选办法，`/api/releases` 用发布时的计划回测
历史预测，`/api/reservation-basis` 把可支持的需求风险转成校区预留依据。

## 业务办理

`staffing.propose` 记录课程组合与所需时段并附上需求依据；`teacher_contracts.confirm`
由教师按整段时间确认，只确认部分时段时按确认比例折算可保障容量；`admissions.publish`
按已确认容量与预测支持的需求上界发布有席位保证的名额。

业务操作以 JSON 对象 POST 到 `/api/workflow/操作名`。三者的既有入参和事务行为保留，新增的校区、时点与需求依据均为可选或由预测计算得出。

- `staffing.propose`
- `teacher_contracts.confirm`
- `admissions.publish`

业务贯通用例见 `tests/test_workflows.py`，其中为可直接运行的本地调用。默认状态存于内存；需要重启后保留时，可运行 `python3 -m app serve --state /tmp/learning-service.sqlite3`。状态库只保留本机操作，不向外部发送数据。
