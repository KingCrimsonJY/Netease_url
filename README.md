# 网易云音乐无损解析

网易云音乐解析与下载服务：歌曲搜索、单曲解析、歌单 / 专辑解析、多音质下载（自动写入标签、封面和歌词），提供网页界面、HTTP 接口和命令行。

本仓库 fork 自 [Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url)，修复了原版在音质、下载缓存、并发、元数据和安全方面的一系列问题，详见文末[主要改动](#与上游相比的主要改动)。

## 授权说明

源代码基于 **MIT License** 开源。上游作者的线上体验站是其独立开发的闭源产品，不属于本仓库。

## 功能

- **10 档音质**，并且如实告诉你实际拿到的是哪一档（请求的音质不可用时会标明"已降级"）
- **下载**：断点续传、按接口返回的 md5 校验、并发安全；网页上显示真实进度，完成后由浏览器直接保存
- **元数据**：标题、艺人、专辑、专辑艺人、曲号/总数、碟号、发行日期、作曲、作词、唱片公司、歌词（可合并翻译）、封面（1200px JPEG），支持 FLAC / MP3 / M4A
- **文件名**：可配置模板，跨平台安全（非法字符转全角、长度按字节控制、保留原名信息）
- 歌单 / 专辑解析（一万首的歌单约 8 秒）、隐私歌单、各种分享链接和短链接
- 账号状态与会员到期提醒、扫码登录
- 访问令牌、限流、默认只监听本机

## 音质

| 参数 | 名称 | 实测格式（以具体歌曲为准） |
| --- | --- | --- |
| `standard` | 标准音质 | MP3 128kbps |
| `higher` | 较高音质 | MP3 192kbps |
| `exhigh` | 极高音质 | MP3 320kbps |
| `lossless` | 无损音质 | FLAC 16/24bit |
| `hires` | Hi-Res音质 | FLAC 24bit / 48~96kHz |
| `jyeffect` | 高清臻音 | FLAC 24bit / 96kHz |
| `sky` | 沉浸环绕声 | FLAC 5.1 声道 |
| `dolby` | 杜比全景声 | EAC3 Dolby Atmos 5.1（.m4a） |
| `vivid` | 臻音全景声 | Audio Vivid（.m4a，多数播放器暂不支持） |
| `jymaster` | 超清母带 | FLAC 24bit / 192kHz |

上游说明：sky、jymaster 需要黑胶 SVIP，其余高于标准的音质需要黑胶 VIP。具体以账号权限为准，网页解析单曲时会显示"账号最高可用"的音质。

歌曲没有所选音质时，网易云会返回其他音质（例如没有杜比版本的歌请求 `dolby` 会得到 `jyeffect`）。接口和网页都会显示实际音质；加 `strict=1` 可让这种情况直接报错。

## 快速开始

需要 Python 3.12 或更高版本。

```bash
git clone https://github.com/KingCrimsonJY/Netease_url.git
cd Netease_url
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.lock
```

之后每次运行前先激活虚拟环境，或直接用 `.venv\Scripts\python main.py`（Linux/macOS 为 `.venv/bin/python main.py`）。直接用系统 Python 运行会报 `No module named 'pathvalidate'` 之类的错误。

### 配置 cookie

只需要 `MUSIC_U`，`os`、`appver` 等设备参数会自动补全。任选一种方式：

- **扫码登录**（推荐）：运行 `python qr_login.py login`，用网易云 App 扫描终端里的二维码；也可以在网页右上角点"扫码登录"
- **手动填写**：登录 [music.163.com](https://music.163.com)，按 F12 → Application → Cookies，复制 `MUSIC_U` 的值，按 `cookie.example.txt` 的格式写入项目目录下的 `cookie.txt`

`python qr_login.py status` 可以查看是否已登录以及会员到期时间。

### 启动

```bash
python main.py
```

打开 http://127.0.0.1:5000 。如需修改端口、缓存位置等，把 `.env.example` 复制为 `.env` 后修改。

## 命令行下载

```bash
# 查看某首歌能拿到的实际音质
python music_downloader.py info 1901371647 --level jymaster

# 下载单首（支持 ID、链接或分享文本）
python music_downloader.py download "https://music.163.com/#/song?id=1901371647" --level lossless --out Music

# 批量下载：文件中每行一个 ID 或链接；目标目录里已有的歌曲会跳过，最后汇总失败原因
python music_downloader.py batch songs.txt --level exhigh --out Music --workers 5 --template "{title}-{artists}"
```

`--strict` 表示实际音质与所选不一致时视为失败。

## 文件名与元数据

- 默认文件名：`歌手 - 歌名.扩展名`。多位歌手用 `, ` 分隔，超过 3 位时显示前 3 位加"等"
- 模板变量：`{artists}` `{artist}` `{title}` `{album}` `{track:02d}` `{disc}` `{year}` `{level}` `{quality_name}` `{id}`
- `/ \ : * ? " < > |` 替换为对应全角字符（`FULLWIDTH_REPLACE=false` 改为 `_`）；Windows 保留名（CON 等）会自动处理；主文件名不超过 180 字节
- 标签字段：FLAC 使用 Vorbis 注释（多位艺人分别写入），MP3 使用 ID3v2.3（多值以 `/` 分隔，`ID3_VERSION=4` 可改为 v2.4），M4A 使用 iTunes 标准字段。另外写入 `NETEASE_SONG_ID`，用来判断同名文件是否为同一首歌
- 封面类型为"封面（正面）"，默认 1200×1200 JPEG（约 300KB）

## 缓存

服务端下载的文件缓存在 `data/cache/{歌曲ID}/{音质}.{扩展名}`，按"歌曲 + 实际音质"区分，不会因为同名而串用。超过 `MAX_CACHE_GB`（默认 20GB）后删除最久未使用的文件。设置 `LIBRARY_DIR` 后，每次下载完成还会按友好文件名把文件硬链接到该目录，适合当作 NAS 音乐库。

## Docker

```bash
mkdir -p data          # Linux 上还需要 chown 1000:1000 data
cp cookie.example.txt data/cookie.txt   # 填入 MUSIC_U，或启动后在网页上扫码登录
docker compose up -d
```

cookie、缓存和日志都在 `./data`。compose 默认只映射到 `127.0.0.1:5000`。

## 安全建议

- 服务只给自己用时保持默认的 `HOST=127.0.0.1`
- 需要让其他设备访问时，**务必设置 `API_TOKEN`**，否则任何能访问该地址的人都能用你的会员账号下载，异常的调用量也可能让账号被风控
- `cookie.txt`、`.env` 已在 `.gitignore` 和 `.dockerignore` 中，不要手动把它们提交或打包

## HTTP 接口

完整说明见 [使用文档.md](使用文档.md)。常用接口：

| 接口 | 说明 |
|---|---|
| `GET /song?id=…&level=…&type=url\|json\|name\|lyric` | 歌曲链接 / 合并信息 / 详情 / 歌词 |
| `GET /search?keyword=…&limit=30&offset=0&type=1` | 搜索 |
| `GET /playlist?id=…&offset=0&limit=500` | 歌单（可分页） |
| `GET /album?id=…` | 专辑 |
| `POST /api/jobs` → `GET /api/jobs/<id>` → `GET /api/files/<id>/<level>` | 异步下载：创建任务、查询进度、取文件 |
| `GET /download?id=…&quality=…&format=file\|json` | 同步下载（兼容旧版） |
| `GET /api/status` | 登录状态、会员到期、缓存占用 |

## 开发

```bash
pip install -r requirements-dev.txt
pytest                      # 离线测试
ruff check . && ruff format --check .
NETEASE_COOKIE_FILE=/path/to/cookie.txt pytest -m live   # 在线测试，需要会员 cookie
```

## 与上游相比的主要改动

- **音质**：补全设备参数（`appver` 等）。浏览器复制的 cookie 不含 `appver`，原版因此会把 hires、jyeffect、sky、dolby、jymaster 全部静默降级为 lossless。现在会如实返回实际音质，并新增 `higher` 和 `vivid` 两档
- **下载缓存**：原版按"歌手 - 歌名"判断是否已下载，会把别的音质、甚至同名的另一首歌当成缓存返回；并发下载同一首歌会写坏文件。现在按"歌曲 + 实际音质"缓存，先下载到临时文件，校验 md5 后再原子提交，并且同一首歌、同一音质同时只会下载一次
- **元数据**：改用 mutagen 写标签，修正曲目总数、专辑艺人、FLAC 封面类型；新增歌词、日期、碟号、作曲作词；封面从数 MB 的 PNG 改为约 300KB 的 JPEG；Audio Vivid 文件也能写入标签
- **文件名**：统一模板，非法字符转全角，浏览器保存的文件名与服务端一致
- **接口**：拿不到链接时返回明确的错误码，不再出现 `success: true` 却没有 `url` 的情况；支持各种链接格式（短链接带 SSRF 防护）；搜索支持分页和类型；歌单接口支持隐私歌单，大歌单不再缺歌
- **安全**：访问令牌、限流、默认只监听本机、修复前端 XSS、Docker 镜像不再打包 cookie
- **其他**：修复无法使用的 `qr_login.py`；依赖升级到 Python 3.12 并修复 78 个已知漏洞；新增 117 个测试和 CI

## 致谢

- [Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url)（上游项目）
- [Ravizhan](https://github.com/ravizhan)
- [NeteaseCloudMusicApi Enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)（设备参数与接口用法参考）
