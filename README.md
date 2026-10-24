# Cloudflare 优选 IP（Docker）

使用官方 CloudflareSpeedTest（CFST）维护指定域名的三个 IP，支持 Linux amd64、arm64。

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

Compose 只运行一个服务。网站镜像版本在 `image` 中修改，CFST 版本在 Web 页面管理。修改配置后执行 `docker compose up -d`；更新镜像先执行 `docker compose pull`。部署机器上填好凭据的 Compose 文件请保留在本机。

## 运行逻辑

1. 启动时如果已有三条有效 DNS 记录，直接检测并进入监控；没有 DNS 记录时才先优选新 IP。
2. 每小时从 Cloudflare 读取当前记录，检测 TCP 平均延迟和下载带宽。全部满足 **延迟 ≤ 100 ms、带宽 > 30 Mbps** 就保留。
3. 任一 IP 不达标时，保留当前三条检测结果，并使用 CFST 输出的全部 CSV 结果（默认对其中 10 个 IP 测下载）；两组结果按 IP 去重，当前检测结果优先，再按下载速度降序、同速按延迟升序排序，取前三个 IP，不再进行第三次测速。
4. 只有去重排序后至少有三个唯一 IP 才同步目标域名的 A 记录，灰云、TTL 60 秒。其他名称和记录类型保留；`RECORD_TYPE` 可改为 `"AAAA"`。
5. 测速或 API 报错、排序后不足三个 IP 时保留原 DNS；429 限流时结束本周期，等待下次检查。页面会显示当前检测结果、CFST 实际输出结果、去重排序结果和 DNS 变更前后记录。

页面中的“读取 DNS、检测当前 IP、优选新 IP、去重排序、写入 DNS”可单独启动，不会自动继续下一步。手动步骤与定时任务共用执行锁；待写入结果有效期 30 分钟，写入前再次核对 DNS 快照。检查间隔可以在页面修改，范围为 1～1440 分钟，修改只在当前容器运行期间生效。

默认把完整的 Cloudflare CIDR 网段交给 CFST，由 CFST 按每个 `/24` 展开并完成延迟筛选；当前官方 IPv4 网段约有 5956 个候选，优选阶段默认下载测试 10 个 IP，终端显示前 10 行。CSV 包含所有通过延迟筛选的结果，其中部分 IP 未参与下载测试，速度零值不能一律解释成下载失败。面板保留 CFST 实际输出的全部结果。测速使用部署机器的出口，真实下载会消耗流量。CFST CSV 的速度按 MiB/s 换算为 Mbps 后比较。

默认参数已经内置：监控延迟 100 ms、带宽 30 Mbps、每小时检查、CFST 下载测试 10 个 IP、单 IP 10 秒、延迟测速线程 200，测速地址为 `https://cf.xiu2.xyz/url`。监控阈值、CFST 下载测试数量、测速时长和测速地址需要调整时，在 Compose 的 `environment` 中添加 `MAX_LATENCY_MS`、`MIN_BANDWIDTH_MBPS`、`DOWNLOAD_COUNT`、`DOWNLOAD_SECONDS` 或 `CFST_URL`，数值使用带引号的字符串；检查间隔直接在页面设置。

需要自定义候选时，创建 `config/candidates.txt`，每行一个 Cloudflare IP 或 CIDR 网段，在 `environment` 添加 `CANDIDATE_FILE: "/config/candidates.txt"`，在 `volumes` 添加 `./config:/config:ro`。候选文件最多 512 行，IPv4 网段由 CFST 展开。来源需在当前 Cloudflare 公布范围内；旧版 ip.txt 的宽泛网段可能超出此范围。IPv6 默认仍用 128 个具体候选，避免展开巨大 IPv6 网段；自定义 IPv6 CIDR 仅支持 /112 或更小范围。CANDIDATE_COUNT 仅用于 IPv6，不再限制 IPv4。

## 状态与数据

```sh
docker compose ps
docker compose logs --tail=100
docker compose down
```

容器名固定为 `cfst-dns`。服务使用容器内部可写目录运行，不创建数据卷；重建或删除容器后，状态、测速记录、已安装 CFST 版本和回滚记录都会清空，并从镜像内置版本重新开始。

## CFST 版本管理

Web 页面的 **CFST** 区域提供：

- **检查更新**：从 XIU2 官方 GitHub 发布读取适合当前架构的版本。
- **安装并切换**：选择官方版本后，自动下载、校验 SHA-256 和架构，并检查实际下载、CSV 和 429 兼容性。
- **切换版本**：选择已安装版本，重新校验后启用。
- **回滚**：恢复上一个版本。

测速或 DNS 任务运行时，版本操作暂不可用。切换成功后直接用于下一次测速，清除旧的待写入结果；失败保留原版本和 DNS。操作不需要停止容器，不需要填写哈希。

版本只在当前容器生命周期内保留。重建容器后会恢复镜像内置版本；官方版本列表缓存 10 分钟，无法连接 GitHub 时仍可切换本次运行期间已安装的版本或回滚。

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

可设置 `CFST_TEST_BIN` 为官方 CFST 路径，启用真实二进制的回环下载和升级兼容性测试。测试无需 Cloudflare 凭据，不写公网 DNS。

CFST 的许可及对应源码见 [THIRD_PARTY.md](THIRD_PARTY.md) 和 [LICENSE-CFST](LICENSE-CFST)。
