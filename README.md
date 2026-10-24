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

Token 需要目标 Zone 的 DNS Read、DNS Edit；自动查找 Zone 时还需 Zone Read。也可以在 `CF_ZONE_ID` 填入 Zone ID。

启动：

```sh
docker compose pull
docker compose up -d
```

在部署机器上访问 <http://127.0.0.1:8789>。远程服务器可先执行 `ssh -L 8789:127.0.0.1:8789 user@server`，再在本机打开此地址。

镜像版本在 `compose.yaml` 的第一处 `image` 中修改，维护工具共用同一镜像。修改配置后执行 `docker compose up -d`；更新镜像先执行 `docker compose pull`。部署机器上填好凭据的 Compose 文件请保留在本机。

## 运行逻辑

1. 启动时优选新前三名，与原有 IP 合并去重，统一复测后选出达标的最终三个 IP。将 `INITIAL_SELECT` 改为 `"false"` 可在启动时先检测已有 IP。
2. 每小时从 Cloudflare 读取当前记录，检测 TCP 平均延迟和下载带宽。全部满足 **延迟 ≤ 100 ms、带宽 > 30 Mbps** 就保留。
3. 任一 IP 不达标时，重新优选新前三名，与旧 IP 合并去重、统一复测，按下载速度降序、同速按延迟升序选择最终三个。旧 IP 仍可入选。
4. 只有最终凑齐三个合格 IP 才同步目标域名的 A 记录，灰云、TTL 60 秒。其他名称和记录类型保留；`RECORD_TYPE` 可改为 `"AAAA"`。
5. 测速或 API 报错、合格 IP 不足时保留原 DNS；429 限流时结束本周期，等待下次检查。

页面中的“读取 DNS、检测当前 IP、优选新 IP、合并复测、写入 DNS”可单独启动，不会自动继续下一步。手动步骤与定时任务共用执行锁；待写入结果有效期 30 分钟，写入前再次核对 DNS 快照。

默认从 Cloudflare 网段均衡抽样 128 个候选，初选最多下载测试 20 个；最终合并阶段复测全部通过延迟筛选的新旧候选。测速使用部署机器的出口，真实下载会消耗流量。CFST CSV 的速度按 MiB/s 换算为 Mbps 后比较。

参数都在 `compose.yaml` 中。需要自定义候选时，创建 `config/candidates.txt`，每行一个 Cloudflare IP，设置 `CANDIDATE_FILE: "/config/candidates.txt"` 并启用文件内的 `./config:/config:ro` 挂载。候选为 3～512 个去重 IP，不接受 CIDR。

## 状态与数据

```sh
docker compose ps
docker compose logs --tail=100
docker compose down
```

`cfst-data` 保存状态，`cfst-bin` 保存 CFST 二进制。更新容器保留这两个命名卷；迁移时保持部署目录或 Compose 项目名，并保留原有卷。`docker compose down -v` 会删除两者。

## CFST 升级

CFST 独立保存在 `/opt/cfst`，首次初始化为官方 v2.3.5。更新网站镜像保留卷内选定的版本。

查看当前版本：

```sh
docker compose run --rm --no-deps cfst-tools status
```

升级前停止主服务，将版本和哈希替换为实际值。SHA-256 对应官方 `cfst_linux_amd64.tar.gz` 或 `cfst_linux_arm64.tar.gz` 整个归档文件：

```sh
docker compose stop cfst-dns
docker compose run --rm --no-deps cfst-tools upgrade --version vX.Y.Z --sha256 填写64位SHA256
docker compose up -d cfst-dns
```

升级会校验哈希、架构、命令参数，并通过回环 HTTP 检查下载、CSV 和 429 输出；失败保留旧版本，不访问 Cloudflare API 或修改 DNS。

回滚：

```sh
docker compose stop cfst-dns
docker compose run --rm --no-deps cfst-tools rollback
docker compose up -d cfst-dns
```

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
