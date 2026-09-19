# 森林康养路线风险联动

天堂寨康养队伍分段行进时的行程接续服务：带队者登记到 / 离段时间，维护人员发布索道停运、路段封闭与疏散通道变化；系统只向仍能安全改线的队伍提供替代连接，已入谷队伍登记等待、折返或人工护送决定；出口与接驳车辆按时间窗互斥占用；支持山区断网补报并在复盘时交代每支队伍如何离开受影响区域。

本服务采用 HTTP 接口和 SQLite 本地文件。运行参数 `PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`fixtures/example.json` 保存不含真实身份的交换示例，`contracts/entities.json` 记录字段约定，`docs/domain.md` 介绍领域规则与范围。

## 本地开发

`make migrate` 初始化数据文件（自动按序应用 `migrations/` 下全部版本），`make test` 运行自动化检查，`make run` 启动服务。`docker compose up --build` 可以启动隔离容器，`APP_PORT` 可调整宿主机端口。

## 接口一览

身份通过请求头传递：带队者 `X-Leader-Ref: <编号>`，维护人员 `X-Staff-Role: MAINTENANCE`。

| 方法 | 路径 | 身份 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 无 | 健康检查 |
| GET | `/api/occupancy` | 带队者或维护 | 全局路线占用：各队当前路段、路段占用队数、封闭状态、索道最新状态 |
| POST | `/api/staff/leaders` `/teams` `/segments` `/exits` `/shuttles` `/plans` | 维护 | 基础数据登记 |
| POST | `/api/staff/ropeway-status` | 维护 | 发布索道 OPEN / SUSPENDED |
| POST | `/api/staff/closures`，`POST /api/staff/closures/{id}/lift` | 维护 | 封闭 / 解封路段 |
| POST | `/api/staff/evacuation-channels`，`POST .../{ref}/state` | 维护 | 发布 / 开关疏散通道 |
| POST | `/api/staff/alternatives` | 维护 | 发布替代连接（可限定步道等级） |
| GET | `/api/staff/review` | 维护 | 复盘汇总 |
| POST | `/api/teams/{team}/checkins` | 本队带队者 | 到 / 离段签到，支持断网补报 |
| GET | `/api/teams/{team}/visitors` | 本队带队者 | 游客名单（他人 403） |
| GET/POST | `/api/teams/{team}/alternatives` | 本队带队者 | 查询可用替代连接 / 确认改线 |
| POST | `/api/teams/{team}/valley-decisions` | 本队带队者 | 已入谷队伍登记 WAIT / RETURN / ESCORT |
| POST | `/api/teams/{team}/reservations`，`POST /api/reservations/{id}/release` | 本队带队者 | 出口 / 接驳车辆时间窗占用与释放 |
| POST | `/api/teams/{team}/outcomes` | 本队带队者 | 登记最终撤离结果 |

时间字段一律使用带时区偏移的 ISO 8601；错误返回 `400/401/403/404/409` 与 `{"error", "message"}`。
