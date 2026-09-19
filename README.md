# 天堂寨分段行程接续

康养队伍沿初、中、高级步道分段行进。索道临时停运时，带队人员登记各路段实际到达与离开时间，维护人员发布封闭与疏散通道变化；系统只对仍能安全改线的队伍提供替代连接，对已入谷队伍记录等待、折返或人工护送决定，并保证同一出口与接驳车辆不被重复占用。游客名单仅所属带队人员可见，断网补报保留原始时间与上传时间，复盘可说明每支队伍最终如何离开受影响区域。

本服务采用 HTTP 接口和 SQLite 本地文件。运行参数 `PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`fixtures/example.json` 保存不含真实身份的交换示例，`contracts/entities.json` 记录字段约定，`docs/domain.md` 介绍来源与范围。

## 主要接口

- `PUT /segments/{ref}`、`PUT /exits/{ref}`、`PUT /connections/{ref}`：登记路段、出口与连接（索道为 `cableway`）。
- `POST /teams`：登记队伍；`PUT|GET /teams/{ref}/members`：游客名单（需 `X-Leader-Ref` 匹配带队人员）。
- `POST /teams/{ref}/checkins`：路段签到，断网补报自动标记 `backfill`；`GET /occupancy`：各路段当前停留队伍。
- `POST /notices`：发布封闭 / 疏散通道变化，返回各队伍分类与替代连接建议。
- `GET /teams/{ref}/options`：队伍当前分类、可用建议与可登记决定；`POST /offers/{ref}/respond`：接受或拒绝建议。
- `POST /teams/{ref}/decisions`：已入谷队伍登记等待 / 折返 / 人工护送。
- `POST /bookings`、`POST /bookings/{ref}/release`：出口与接驳车辆占用，时段冲突返回 409。
- `GET /review?notice_ref=`：复盘，逐队说明最终离场方式。

## 本地开发

`make migrate` 初始化数据文件，`make test` 运行现有自动化检查，`make run` 启动服务。`docker compose up --build` 可以启动隔离容器，`APP_PORT` 可调整宿主机端口。
