# Cloudflare 优选 IP 测试台

**Docker 自动维护模式**：部署时填写域名和 API Token，调用官方 CFST（初始 v2.3.5）优选三个 IP 并同步 DNS；每小时从 Cloudflare 重读并检查，延迟 ≤ 100 ms 且带宽 > 30 Mbps 时保留，任一不达标则重新优选。重选时将原始三个与新优选前三名合并去重、统一复测排序，再将达标的最终前三名写入 DNS。页面支持分别启动“读取 DNS、检测当前 IP、优选新 IP、合并复测、写入 DNS”，手动步骤不自动继续后续步骤；与定时任务串行执行。支持 DNS 批量更新、失败保留原记录。完整步骤见 [DOCKER.md](DOCKER.md)，部署入口为 `compose.yaml`。以下章节介绍原有的本地手动测速模式，两者的后端算法不同。

CFST 二进制保存在独立的 `cfst-bin` 数据卷，初始版本 v2.3.5；支持指定版本独立升级、校验和回滚，重建网站镜像会保留卷内已选定版本。命令见 [CFST 独立升级与回滚](DOCKER.md#cfst-独立升级与回滚)。

默认使用 Docker Hub 镜像 `cotelforce/cloudflare-ip-tester:3.1.0`。配置 `.env` 中的域名和 Token 后，执行 `docker compose up -d` 即可部署；可用 `CFIP_IMAGE` 指定其他镜像版本。需要源码构建时使用 `docker compose -f compose.yaml -f compose.build.yaml up -d --build`。GitHub 自动测试及 Docker Hub 双架构镜像发布见 [PUBLISH.md](PUBLISH.md)。

本地运行的中文测速网站。读取 Cloudflare 控制台中指定域名的 A / AAAA 记录，勾选候选 IP，逐个下载测速，实时查看进度并导出结果。使用 Python 标准库和原生 HTML/CSS/JS，无第三方依赖、数据库或付费接口。

## 启动

需要 Python 3.10 或更高版本，不需要安装第三方依赖：

```powershell
cd F:\codex\daily\cloudflare-ip-tester
python .\app.py
```

然后打开 <http://127.0.0.1:8787>。也可以双击 `start.bat`，或运行 `run.ps1`。

端口被占用时可改用 `python app.py --port 8790`。按 Ctrl+C 关闭前台服务。

## 使用步骤

1. 填入候选 IP 的域名和 API Token，域名没有预设值。
2. 点击“读取 DNS 中的 IP”，后端自动从子域逐级查找父域 Zone；也可手动填 Zone ID。
3. 勾选准备测试的 IP，取消勾选已淘汰的地址。
4. 选择下载 URL、1～3 轮测试、5～30 秒单次时限，点击“开始测速”。
5. 结果逐轮更新并按速度排序，可以停止任务、导出选中 IP 的 TXT 或测速结果 CSV。

**域名是获取候选 IP 的来源，下载 URL 是测速文件来源，两个设置互相独立。** 优选域名只存 A 记录也能使用，无需自己提供 HTTP 服务。

## API Token 权限

建议创建一个只读 API Token，至少需要：

- `Zone - Zone - Read`（不填写 Zone ID 时用于查找 Zone）
- `Zone - DNS - Read`

如果手动填写正确的 Zone ID，可以不授予 Zone 查询权限，但仍需要 DNS Read。

使用 API Token（Bearer），不支持 Global API Key + 邮箱。将 Token 权限和资源范围限制到所需 Zone。权限可参考 [Cloudflare 官方权限表](https://developers.cloudflare.com/fundamentals/api/reference/permissions/)。

Token 仅在读取记录请求中转发到 Cloudflare 官方 API；不存入文件、日志、浏览器存储、结果快照或测速任务。读取成功后页面清空 Token。后端记录和结果仅保存在内存中，约 30 分钟过期；重启服务会清空。页面刷新不会恢复上次任务，因此运行中请保持页面打开。

默认只监听 `127.0.0.1`，检查 Host / Origin，写入接口要求同源自定义请求头；任务和 DNS 快照按临时会话隔离。不建议直接将本地标准库服务器代理到公网。公开部署需另做登录、HTTPS、资源配额和服务管理。

## 测速地址

默认地址是：

```text
https://speed.cloudflare.com/__down?bytes=10000000
```

也可以使用自己的测速文件 URL，例如：

```text
https://cf.example.com/speed/100mb.bin
```

测速 URL 的域名用于 TLS SNI、证书验证和 HTTP Host；实际 TCP 连接强制拨到你勾选的候选 IP，不使用测速 URL 的普通 DNS 结果。测速请求不会携带 Cloudflare API Token，不会跟随重定向。

如果测速源返回 429，立即停止整项任务并显示限流，保留已完成结果；未测 IP 不记成慢速，不会自动轮换 IP 重试限流请求。403、重定向、证书失败、网页/JSON 响应、过小文件不会作为有效速度。请使用稳定的直接下载地址。

## 测速口径

- **串行下载**：一次只有一个 IP 下载文件，避免多个候选争用带宽。每轮按 IP 列表完整测试，再进入下一轮。
- **下载速度**：有效文件字节 / 接收文件正文耗时，使用十进制 MB/s（1,000,000 字节/秒），不含 TCP/TLS 建连和响应头等待时间。不会统计 chunked 传输的分块标记。
- **平均速度**：仅对有效轮次取算术平均，失败轮次保留状态而非当作 0。表格显示有效轮数，避免把 1/3 成功当作三轮稳定。
- **首字节**：从开始连接到收到 HTTP 响应头的时间；每轮内部同时记录 TCP、TLS 建连时间。
- **单次时限**：包含连接、TLS、等待响应、下载。已接收至少 64 KiB 时，限时结束可作为带“限时采样”标记的部分样本；取消的轮次不计入排名。
- **短样本**：少于 5 MB 或正文下载短于 2 秒会显示标记。高速线路可手动改用更大的自有文件，降低短样本波动。

## 限制

- 最多载入 100 个去重 IP，支持分页；不会展开 CNAME。
- 仅允许测试本次从 API 获取的、位于 Cloudflare 官方代理网段的公网 IP，其他地址显示但跳过。网段列表来源为 [Cloudflare IP Ranges](https://www.cloudflare.com/ips/)，后续网段变化需更新 `cloudflare.py`。
- 支持 IPv4 和 IPv6；本机没有 IPv6 出站线路时，对应记录会显示连接失败。
- 橙云记录读取的是控制台的记录内容，不是公共 DNS 查询返回的边缘 IP；界面会标明。
- 一次只允许运行一个任务，最多 3 轮，单次最多 30 秒 / 100 MB。
- 测试在 Python 后端发起；本机代理/TUN、出口路由会影响结果。不是浏览器访问者的远程测速，也未内置 XIU2 的 CFST 二进制，数值不保证与 CFST 的采样算法一致。
- 这是 Python 后端网站，不能直接当作静态 Pages 网站上传。需要运行 Python 的本机或服务器。部署在 Cloudflare 边缘也无法代表你的宽带线路。

## 验证

```powershell
python -m unittest -v test_app.py
node --check static/app.js
```

离线回归测试使用本地 HTTP 文件服务及模拟 Cloudflare 响应，覆盖分块计数、完整/中断下载、错误状态、时限、取消、Zone 父域查找、分页、会话和请求来源检查，不需要真实 Token。

可选界面测试夹具：`python test_app.py --preview` 在 8788 端口提供**模拟**记录与速度。仅供开发验证，Token 为 `ui-fixture-token`，不会请求 Cloudflare。正常启动 `app.py` 不包含此模式，也不会导入测试代码。
