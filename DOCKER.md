# Docker + CFST 自动优选

部署机器需安装 Docker Engine / Docker Desktop 和 Compose v2。支持 Linux amd64、arm64 容器，Windows 使用 Linux containers。

默认从 Docker Hub 拉取 `cotelforce/cloudflare-ip-tester:3.1.0`，支持 Linux amd64、arm64。可在 `.env` 设置 `CFIP_IMAGE` 指定其他版本；默认部署无需本机构建。

## 配置并启动

进入项目目录。如果部署机器有 Python，可以运行交互配置：

```sh
python configure.py
```

依次填写优选域名、API Token、可选 Zone ID。Token 输入不回显，配置文件以仅当前用户可读写的权限创建（Windows 权限由系统 ACL 决定）。已有 `.env` 不会被覆盖。

也可以直接把 `.env.example` 复制为 `.env`，编辑必填的两项：

```dotenv
CF_DOMAIN=cf.example.com
CF_API_TOKEN=填写你的Token
```

Linux / macOS：`cp .env.example .env`。PowerShell：`Copy-Item .env.example .env`。

域名没有默认值，请将 `CF_DOMAIN` 填为你要管理的完整域名；页面显示这个配置值。修改后运行 `docker compose up -d --force-recreate` 生效。上面的 `cf.example.com` 仅为格式示例。

Token 的资源范围限制到该域名所属 Zone，授予：

- Zone / DNS / Read
- Zone / DNS / Edit
- Zone / Zone / Read（自动找 Zone 时需要；填写 CF_ZONE_ID 后可省略）

域名必须在这个 Token 可操作的 Cloudflare Zone 下。只支持别人提供的子域但没有 Zone API 权限时，不能更新其 DNS。

启动：

```sh
docker compose pull
docker compose up -d
docker compose logs -f --tail=100
```

操作页面：<http://127.0.0.1:8789>。默认仅绑定部署机器回环地址，远程服务器可用 SSH 转发 `ssh -L 8789:127.0.0.1:8789 user@server` 后在本机打开。页面支持查看状态和启动单独步骤，配置从部署文件读取。通过 `127.0.0.1` 或 `localhost` 访问；操作接口检查 Host、Origin 和页面请求头。

`.env` 含长期运行所需的 Token，是部署凭据文件；不要上传、公开或加入 Git。它已被 `.gitignore` 和 `.dockerignore` 排除。Token 不传入 CFST 子进程、不写日志或状态 JSON，也不会发到测速文件服务器。还可使用 `CF_API_TOKEN_FILE` 读取你在 Compose 中挂载的 Docker secret（需要自行补充对应 secrets 挂载，并清空 CF_API_TOKEN）。

## 执行逻辑

1. **启动时优选**：读取指定域名的当前记录，运行数据卷中已选定的官方 CFST（首次初始化为 v2.3.5），从候选中选出符合标准的新前三名。若已有 IP，将原有 IP 与新前三名合并去重，统一再用 CFST 复测，按这次复测的速度降序选出最终三个合格 IP；无原有记录时直接采用首次优选结果。设置 `INITIAL_SELECT=false` 可以改为启动时先复查已有三个 IP。
2. **必须满足两个条件**：CFST TCP 平均延迟 **≤ 100 ms**，下载带宽 **> 30 Mbps**。对应环境变量是 `MAX_LATENCY_MS=100` 和 `MIN_BANDWIDTH_MBPS=30`。CFST CSV 虽标为 MB/s，但实际除以 1024²；程序按 `CSV速度 × 1024² × 8 ÷ 1,000,000` 换算为 Mbps 后比较。CSV 本身仅两位小数，因此边界存在 CFST 输出精度。
3. **写入 DNS**：有完整三个合格 IP 才提交 Cloudflare batch 请求。默认将目标域名的 **A 记录集合**同步为这三个 IP，`proxied=false`（灰云），TTL 60 秒。相同 IP 的已有记录尽量保留，移除同名同类型的其余旧记录和重复记录。不同名称、TXT、MX、AAAA 等其他类型不改。不要将同一个域名的 A 记录同时交给别的 DDNS 程序管理。
4. **每小时复查**：从 Cloudflare API 重新读取 DNS，使用 CFST 检测当前三个 IP。全部达标就不写 DNS；任一 IP 不达标、少于/多于三个、重复或变为橙云时，执行一次重新优选。**原始三个 IP + 新优选前三名 → 去重 → 同一轮 CFST 复测 → 按下载速度降序、同速按延迟升序 → 取达标的最终前三名写入 DNS**。正常为最多六个去重 IP，旧 IP 可以继续入选；先前不达标的旧 IP 复测恢复后也可入选。只比较新旧候选本次复测结果，不沿用旧测速值。若原记录异常地超过三个，会让所有有效旧 IP 参加最终比较。
5. **未找到三个合格 IP**：新优选不足三个时，仍可与旧 IP 合并参与最终比较；最终不足三个合格 IP 才保留原记录，状态显示“合格 IP 不足”，下一小时再检查，不循环刷测。API 失败、测速报错、CFST 超时也不会先清空 DNS。
6. **429 限流**：测速源或 Cloudflare API 返回 429 时终止本周期，不自动换 IP/测速源重试，等下个周期。CFST 输出通过 `-debug` 检测；独立升级时会检查该输出是否兼容。

检查间隔以本轮开始时间为基准，为 3600 秒；任务串行。如果单次任务超过间隔，完成后再等一个间隔。重启会启动一个新周期，不补跑停机期间的任务。

## 单独运行步骤

页面提供五个按钮，点击只执行对应步骤，不自动继续后续步骤：

| 按钮 | 执行内容 | 前置结果 |
|---|---|---|
| 读取 DNS | 从 Cloudflare 读取当前记录，保存本次快照 | 无 |
| 检测当前 IP | 使用 CFST 检测已读取的有效 IP，显示达标情况 | 已读取 DNS，且有可测 IP |
| 优选新 IP | 从候选池测速，保存达标的新前三名 | 无，可单独启动 |
| 合并复测 | 原有有效 IP 与新前三名去重、统一重测并排序，显示待写入的最终三个 | 已读取 DNS、已完成优选 |
| 写入 DNS | 将最近一次合并复测的最终前三名同步到域名并核验 | 完整三个达标结果 |

检测不达标时不会自动重选，优选和复测也不会自行写入；需要分别点击后续按钮。可重复运行任一步骤。执行读取、检测或优选会清除此前待写入的最终排名，需要再做合并复测。优选结果不足三个时也可以合并旧 IP 复测，最终仍须有三个达标 IP 才能写入。没有旧记录时，手动合并复测会重新测试新候选。

DNS 快照、初选结果和最终复测结果均只在本次进程内有效，超过 30 分钟须重新执行相应步骤。容器重启后历史结果仅供查看，不能直接写入。写入前仍会重读 DNS，发现他人修改就拒绝覆盖；写入尝试后会消耗这份待写入结果，避免 API 超时后盲目重复提交。

手动步骤与每小时自动维护共用执行锁，运行中其他按钮禁用。到达自动检查时间时，若手动步骤尚未完成，会等其结束再运行。手动操作不关闭或重置每小时自动检查；新自动周期会重新读取、检测和优选，不沿用未完成的手动步骤结果。

批量 DNS 操作在 Cloudflare 数据库中作为一个事务执行，但全球 DNS 传播不是原子的，缓存受 TTL 影响。写入前会重读快照，发现测速期间有他人修改则停止覆盖；写入后核验。API 没有提供本程序使用的条件写入锁，仍建议指定一个由本服务独立管理的优选子域。写入请求超时不能确定是否已生效，本周期不盲目重试，下一次以 API 读到的状态为准。

## 候选来源与测速参数

默认优选池：从 `cloudflare.py` 内置的 Cloudflare 官方代理网段中，按网段均衡随机抽取 128 个去重 IP（包含当前有效 Cloudflare IP）。这不是扫描全部 Cloudflare 地址。使用 CFST 的 TCP 延迟筛选后，最多下载测试 20 个延迟较低的候选，再在其中选择速度前三名，与原有 IP 合并去重进行最终复测；最终阶段下载测试所有通过延迟筛选的新旧候选，不受 `DOWNLOAD_COUNT` 的初选数量限制。未实际下载的结果不会入选。只保证测试候选中的前三名，不能保证全球最快。

若希望从自己收集的 IP 中优选，在 `config/candidates.txt` 中每行填一个完整 IP（支持 `#` 注释），然后在 `.env` 设置：

```dotenv
CANDIDATE_FILE=/config/candidates.txt
```

自定义候选文件是新 IP 初选阶段的完整来源，不会额外加入旧 IP 或随机 IP；至少 3 个、最多 512 个去重地址，只允许 Cloudflare 公网网段和所选地址族。最终比较阶段仍会加入原有有效 IP。不接受 CIDR，避免无意扩大测试范围。

可调整参数：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| CHECK_INTERVAL_SECONDS | 3600 | 检查间隔，最小一小时 |
| RECORD_TYPE | A | 维护 IPv4；可设 AAAA 维护 IPv6，另一类型保留 |
| CANDIDATE_COUNT | 128 | 随机候选数，3～512，自定义文件时以文件为准 |
| DOWNLOAD_COUNT | 20 | 每次优选最多下载测试数，3～50 |
| CFST_THREADS | 8 | TCP 探测并发，1～16；下载仍串行 |
| DOWNLOAD_SECONDS | 10 | 单 IP 下载时限，5～30 秒 |
| DNS_TTL | 60 | DNS 记录 TTL，60～86400 秒 |
| WEB_PORT | 8789 | 部署机的本地状态页端口 |

默认测速 URL 为 `https://speed.cloudflare.com/__down?bytes=200000000`。可设置 `CFST_URL` 为自己的稳定大文件直链；测速域名和优选 DNS 域名互相独立，实际连接到待测 IP。文件过小、下载过快、测速源限制都会影响 CFST 结果；不要用很小的文件测试高速线路。CFST 本身的重定向处理保留。

带宽检测需要真实下载，流量会持续消耗。仅每小时复查三个 IP，默认 200 MB 文件全部下载时上限约 **600 MB/轮、14.4 GB/天**；重新优选额外消耗流量。运行在哪台宿主机，测到的就是该机器的出口线路，不能代表访问网页者的宽带。

## 运维、演练与验证

```sh
# 状态和日志
docker compose ps
docker compose logs --tail=100

# 修改 .env 后重新创建容器
docker compose up -d --force-recreate

# 停止服务
docker compose down
```

状态及最近 30 条事件保存到 `cfst-data` 命名卷，包含域名、IP、测试结果与时间，不含凭据。同一数据卷有进程锁，避免两个自动任务同时运行。CFST 可执行文件另存于 `cfst-bin` 命名卷。

## CFST 独立升级与回滚

此功能升级的是 **CFST 二进制本身**。CFST 放在独立的 `cfst-bin` 命名卷，容器内路径为 `/opt/cfst`；`releases/` 保留各版本文件和校验信息，`current.json` 记录当前版本与上一个版本。首次启动从镜像自带的官方 v2.3.5 初始化。之后重启、重新创建容器、重建网站镜像都不会覆盖已选定的 CFST 版本。启动时检查当前二进制的 SHA-256 和架构，再将实际路径及版本传给自动测速进程。

从旧部署迁移，替换项目文件后运行 `docker compose pull` 和 `docker compose up -d`，保持相同部署目录、Compose 项目名、`.env` 及数据卷，继续使用已有 `cfst-data` 和 `cfst-bin`。没有二进制卷时会创建并初始化。维护工具与主服务使用同一镜像，工具容器不加载 Cloudflare Token。

查看当前版本、上一个版本和二进制 SHA-256（服务运行中也可查看）：

```sh
docker compose run --rm --no-deps cfst-tools status
```

升级时，指定目标发布版本及对应 **Linux 归档文件**的 SHA-256。以下 `vX.Y.Z` 和 `填写64位SHA256` 是占位符，需要替换为实际值；哈希针对 `cfst_linux_amd64.tar.gz` 或 `cfst_linux_arm64.tar.gz` 整个文件，不是解压后的 cfst 文件：

```sh
docker compose stop cfst-dns
docker compose run --rm --no-deps cfst-tools upgrade --version vX.Y.Z --sha256 填写64位SHA256
docker compose up -d cfst-dns
```

下载仅使用 XIU2/CloudflareSpeedTest 官方 GitHub Release，按容器 CPU 架构取 Linux amd64 / arm64 归档。先校验归档哈希与 ELF 架构，再检查版本和命令行参数、通过本机回环 HTTP 文件检测真实下载及 CSV 格式，并验证 429 输出。兼容性检查不调用 Cloudflare API、不访问公网测速源、不修改 DNS。全部通过后才原子切换当前版本指针；失败会保留原有版本。网站容器再次启动后使用新 CFST，无需重新构建镜像。

回滚到上一个版本：

```sh
docker compose stop cfst-dns
docker compose run --rm --no-deps cfst-tools rollback
docker compose up -d cfst-dns
```

回滚会再次校验并检查上一个版本，然后交换当前与上一个版本，因此也可再次运行回滚切回。升级和回滚共用自动服务的进程锁；服务未停止时会拒绝操作。只有版本查看命令可以在服务运行期间执行。

`docker compose down` 保留这两个命名卷；**`docker compose down -v` 会同时删除状态和 CFST 版本卷**。备份或迁移时请保留 `cfst-data` 与 `cfst-bin`，并保持相同的 Compose 项目名；换项目名会创建另一套卷。二进制卷需要由容器 UID/GID `10001:10001` 读写，默认命名卷由镜像目录权限初始化。旧版本不会自动清理。

在第一次允许写 DNS 前，可以演练一轮（仍会调用真实 Cloudflare API 并下载测速）：

```sh
docker compose pull
docker compose run --rm --no-deps cfst-dns --once --dry-run
```

演练时不要同时启动常驻容器；数据卷的互斥锁会拒绝第二个任务。演练只提出 IP，不修改 DNS。之后运行 `docker compose up -d` 即开始正常自动维护。

不配置凭据也可在本机预览页面：`python automation.py --dashboard-only --port 8790`。此模式显示“预览模式”，五个按钮禁用，不会调用 Cloudflare、运行 CFST 或写 DNS；使用独立端口，避免占用 Docker 的 8789。

预览默认显示“未配置”，也可通过进程环境变量 `CF_DOMAIN` 指定预览域名；预览命令不会自行读取 `.env`。Docker 正常运行时由 Compose 加载 `.env` 中的配置。

## 源码构建

开发或修改程序时，可以叠加源码构建配置：

```sh
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

这一配置将主服务和 CFST 维护工具都切换到本地镜像 `cfipdesk-cfst:3.1`。后续查看状态、执行演练、升级 CFST 等操作也使用同样的 `-f compose.yaml -f compose.build.yaml` 参数。现有的 `compose.hub.yaml` 保留兼容，与默认配置使用相同的 Docker Hub 镜像及数据卷。

离线测试：

```sh
python -m unittest -v test_automation.py test_cfst_manager.py test_app.py
node --check static/auto.js
```

可选真实二进制契约测试：设置 `CFST_TEST_BIN` 为官方 cfst / cfst.exe 路径，运行 `python -m unittest -v test_automation.RealCFSTContract test_cfst_manager.PersistentCFSTTests.test_upgrade_probe_with_official_cfst`。它仅测试帮助输出和回环地址上的文件服务，无需 Token，不写公网 DNS。

Docker 镜像自带的 CFST 初始版本为官方 v2.3.5，amd64 / arm64 的归档 SHA-256 固定在 `cfst_manager.py`，由 `docker/fetch_cfst.py` 在构建时下载并验证。后续版本由上述维护命令独立安装，不随网站镜像自动升级或降级。构建需要能访问 Docker Hub、GitHub 及其官方文件分发域名，CFST 升级需要访问 GitHub 官方发布文件，不使用第三方下载源。原项目 GPLv3 许可证保存在 `LICENSE-CFST`；分发修改版/镜像时应遵守其许可。

参考：[CFST 源码及发布](https://github.com/XIU2/CloudflareSpeedTest/tree/v2.3.5)、[Cloudflare batch 说明](https://developers.cloudflare.com/dns/manage-dns-records/how-to/batch-record-changes/)、[Cloudflare 代理网段](https://www.cloudflare.com/ips/)。
