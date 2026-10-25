# Cloudflare 优选 IP（Docker）

使用官方 CloudflareSpeedTest（CFST）测速并维护指定域名的 DNS 记录。记录数量可配置，支持 Linux amd64、arm64。

- [源码](https://github.com/mwyou/cloudflare-ip-tester)
- [Docker Hub 镜像](https://hub.docker.com/r/cotelforce/cloudflare-ip-tester)

## 部署

安装 Docker 和 Compose v2，下载 [compose.yaml](https://raw.githubusercontent.com/mwyou/cloudflare-ip-tester/main/compose.yaml) 到一个部署目录。所有配置直接写在该文件中，必填项为：

```yaml
environment:
  CF_DOMAIN: "你的完整优选域名"
  CF_API_TOKEN: "你的CloudflareToken"
```

Token 需要目标 Zone 的 DNS Read、DNS Edit；自动查找 Zone 时还需 Zone Read。有 Zone ID 时，可以在 Compose 的 `environment` 中添加 `CF_ZONE_ID: "你的ZoneID"`。

启动：

```sh
docker compose pull
docker compose up -d
```

端口映射为 `18789:8787`，监听宿主机所有网络接口。在浏览器访问 `http://服务器IP:18789`，部署机器本机也可访问 <http://127.0.0.1:18789>。Web 支持通过 IP 或域名访问，操作接口保留同源校验。

Compose 只运行一个服务。网站镜像版本在 `image` 中修改，CFST 固定使用镜像自带的官方版本。修改配置后执行 `docker compose up -d`；更新镜像先执行 `docker compose pull`。部署机器上填好凭据的 Compose 文件请保留在本机。

## 运行逻辑

1. 第一次读取 DNS 时自动采用目标域名现有 A/AAAA 记录条数；没有记录时默认使用 3 条。页面可将目标数量改为 1～10 条。已有记录数量和页面设置会决定后续检测、排序和写入数量。
2. 默认每小时从 Cloudflare 读取当前记录，检测 TCP 平均延迟和下载带宽。全部满足页面配置的检测标准就保留，默认 **延迟 ≤ 100 ms、带宽 > 30 Mbps**。
3. 任一 IP 不达标时，保留当前记录的检测结果，并使用 CFST 输出的全部 CSV 结果（默认对其中 10 个 IP 测下载）；两组结果按 IP 去重，当前检测结果优先，再按下载速度降序、同速按延迟升序排序，按目标数量取前 N 个 IP，不再进行第三次测速。
4. 只有去重排序后至少有目标数量个唯一 IP 才同步目标域名的 A 记录，灰云、TTL 60 秒。其他名称和记录类型保留；`RECORD_TYPE` 可改为 `"AAAA"`。
5. 测速或 API 报错、排序后不足目标数量个 IP 时保留原 DNS；429 限流时结束本周期，等待下次检查。测速结果依次显示“当前 IP 检测、CFST 新 IP 结果、综合排序”，当前 IP 检测完整显示；新 IP 结果与综合排序默认显示前三行，可分别展开；浏览长列表时标题与“收起”按钮会吸附在顶部。新 IP 区域排除本轮优选开始时已有的 DNS IP；综合排序保留当前检测结果与新结果，按 IP 去重。当前 DNS 单独显示；历史默认保留最近两次变更前的记录和变更时间，保留份数可在页面设置为 0～10 份；0 表示关闭，调小份数会立即移除更早的记录。未改变 IP 的同步和写入失败不会追加历史。

页面中的“读取 DNS、检测当前 IP、优选新 IP、综合排序、写入 DNS”可单独启动，不会自动继续下一步。手动步骤与定时任务共用执行锁；待写入结果有效期 30 分钟，写入前再次核对 DNS 快照。运行设置在桌面端横排显示固定检查间隔（1～1440 分钟）、随机波动、检测标准、DNS 记录数量（1～10 条）和旧记录份数（0～10 份）。检测标准可直接填写延迟上限（1～2000 ms）与带宽下限（0.1～10000 Mbps），保存后立即更新达标判断，下一轮监控使用新标准。窄屏自动换行。

固定间隔与随机波动分别设置。随机波动默认关闭；启用后在 **1～10000 秒** 的轴上拖动两个端点选择区间。每轮自动检查一开始就独立均匀抽取一个整数秒数，按 **下一次检查时间 = 本轮开始时间 + 固定间隔 + 随机波动** 生成并展示计划时间，检测期间一直保留，不在检测结束后重算。例如 60 分钟 + 10～300 秒，周期开始间隔为 3610～3900 秒。如果本轮耗时超过计划时间，完成后启动下一轮，任务不会并发执行。修改固定间隔或随机波动后，从保存时重新安排下一次检查。未保存的输入和拖动位置不会被状态刷新覆盖。

当前 DNS、历史记录和每个结果表中的 IP 后均有“测速”按钮，调用内置 CFST，仅对该 IP 进行延迟和下载测试。该 IP 已有的结果行会更新，综合排序会按新数值重新排列；历史 IP 的测试结果仅显示在历史条目旁，不会插入当前或新 IP 结果。单 IP 测速不会读取或修改 DNS，也不会优选其他 IP，不改变自动检查计划。单测会清除旧的待写入方案，若要应用新结果，请重新执行综合排序，再写入 DNS。所有测速和自动任务共用执行锁。

运行设置无需写入 Compose。检查间隔、随机波动、检测标准、历史份数及结果保存在容器内部状态文件中，普通进程重启可恢复，重建或删除容器后清空；DNS 记录数量在启动时重新采用现有 DNS 数量。


默认把完整的 Cloudflare CIDR 网段交给 CFST，由 CFST 按每个 `/24` 展开并完成延迟筛选；当前官方 IPv4 网段约有 5956 个候选，优选阶段默认下载测试 10 个 IP，终端显示前 10 行。CSV 包含所有通过延迟筛选的结果，其中部分 IP 未参与下载测试，速度零值不能一律解释成下载失败。后台保留完整 CSV 结果，展开新 IP 区域可查看排除本轮已有 DNS IP 后的所有行。测速使用部署机器的出口，真实下载会消耗流量。CFST CSV 的速度按 MiB/s 换算为 Mbps 后比较。

默认参数已经内置：监控延迟 100 ms、带宽 30 Mbps、每小时检查、CFST 下载测试 10 个 IP、单 IP 10 秒、延迟测速线程 200，测速地址为 `https://cf.xiu2.xyz/url`。检查间隔和监控阈值直接在页面设置。CFST 下载测试数量、测速时长和测速地址需要调整时，在 Compose 的 `environment` 中添加 `DOWNLOAD_COUNT`、`DOWNLOAD_SECONDS` 或 `CFST_URL`，数值使用带引号的字符串。`MAX_LATENCY_MS` 和 `MIN_BANDWIDTH_MBPS` 仍可设置首次启动默认值，之后以页面保存的设置为准。

需要自定义候选时，创建 `config/candidates.txt`，每行一个 Cloudflare IP 或 CIDR 网段，在 `environment` 添加 `CANDIDATE_FILE: "/config/candidates.txt"`，在 `volumes` 添加 `./config:/config:ro`。候选文件最多 512 行，IPv4 网段由 CFST 展开。来源需在当前 Cloudflare 公布范围内；旧版 ip.txt 的宽泛网段可能超出此范围。IPv6 默认仍用 128 个具体候选，避免展开巨大 IPv6 网段；自定义 IPv6 CIDR 仅支持 /112 或更小范围。CANDIDATE_COUNT 仅用于 IPv6，不再限制 IPv4。

## 状态与数据

```sh
docker compose ps
docker compose logs --tail=100
docker compose down
```

容器名固定为 `cfst-dns`。服务使用容器内部可写目录运行，不创建数据卷；重建或删除容器后，状态、页面设置和测速记录都会清空。CFST 始终从镜像内置路径运行。

## 内置 CFST

镜像自带官方 Linux CFST v2.3.5，按 amd64 / arm64 自动匹配架构。构建时校验归档 SHA-256，启动时校验内置版本、架构和二进制 SHA-256；不需要 CFST 数据卷、版本选择或升级设置。页面和 API 均不提供 CFST 下载、切换或回滚；更新 CFST 随网站镜像一起发布。

## 源码与发布

仓库仅包含 Docker 版：`app/` 为服务及页面，`docker/` 为构建辅助程序，`tests/` 为相关测试，`.github/workflows/` 为自动测试和镜像发布。

本地构建：

```sh
docker build -t cfipdesk-cfst:local .
```

然后将 `compose.yaml` 第一处 `image` 改为 `cfipdesk-cfst:local`，执行 `docker compose up -d`。Compose 配置不进入镜像构建上下文。

GitHub 推送 `main` 会运行测试和双架构容器检查；推送 `v主版本.次版本.修订版本` 标签会发布版本镜像和 `latest`。发布使用仓库变量 `DOCKERHUB_USERNAME`、`DOCKERHUB_IMAGE` 和 Secret `DOCKERHUB_TOKEN`，当前仓库已配置。

离线测试：

```sh
PYTHONPATH=app python -m unittest discover -s tests -p 'test_*.py' -v
node --check app/static/auto.js
```

可设置 `CFST_TEST_BIN` 为官方 CFST 路径，启用真实二进制的回环下载和内置版本兼容性测试。测试无需 Cloudflare 凭据，不写公网 DNS。

CFST 的许可及对应源码见 [THIRD_PARTY.md](THIRD_PARTY.md) 和 [LICENSE-CFST](LICENSE-CFST)。
