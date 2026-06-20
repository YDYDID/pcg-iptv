# 广东/上海 IPTV 抓取脚本

在CODEX的协助下，写了新的IPTV抓取脚本
用于抓取广东电信 IPTV、广东移动 IPTV、上海电信 IPTV 的频道列表、XMLTV 节目表和回放地址，并生成可给 APTV、TiviMate、Kodi、udpxy、rtp2httpd 等工具使用的 M3U/XMLTV 文件。
广东移动IPTV同时支持中兴和华为回放途径（华为回放途径必须填写IPTV盒子账号、密码、MAC地址，有效期24小时，所以必须配好定时任务每天生成）
广东电信IPTV要求要有IPTV盒子账号、密码、MAC地址
上海电信IPTV要求要有IPTV盒子账号、SN、MAC地址
回放格式有些播放器可能不兼容，可以试试经过rtp2httpd代理的。

三个脚本都是单文件 Python 实现，只依赖 Python 标准库，适合放在 OpenWrt/ImmortalWrt 或内网服务器上定时运行。

## 脚本和输出

| 地区/运营商 | 脚本 | 主要输出 |
| --- | --- | --- |
| 广东电信 IPTV | `gdctiptv.py` | `gdctiptv.m3u`、`gdctiptv2.m3u`、`gdctiptv3.m3u`、`gdctiptv4.m3u`、`gdctepg.xml`、`gdctepg.xml.gz` |
| 广东移动 IPTV | `gdcmiptv.py` | `gdcmiptv.m3u`、`gdcmiptv2.m3u`、`gdcmiptv3.m3u`、`gdcmiptv4.m3u`、`gdcmiptv5.m3u`、`gdcmepg.xml`、`gdcmepg.xml.gz` |
| 上海电信 IPTV | `shctiptv.py` | `shctiptv.m3u`、`shctiptv2.m3u`、`shctepg.xml` |

分别对应原始的RTSP/组播链接，以及标准的UDPXY协议，和rtp2httpd。

## 使用方法

1. 确认运行设备能访问对应运营商 IPTV 专网。
2. 打开对应脚本，在顶部“用户配置”或“用户可配置项”区域填写自己的账号、MAC、STBID、SN、代理地址等配置。
3. 运行脚本：

```bash
python3 gdctiptv.py
python3 gdcmiptv.py
python3 shctiptv.py
```

## 配置位置

### 广东电信

编辑 `gdctiptv.py` 顶部配置：

```python
USER_ID = ""
PASSWORD = ""
MAC = ""
IMEI = ""
ADDRESS = ""
```

常见还需要按自己的网络修改：

```python
M3U_XMLTV_URL = "http://10.10.10.250:33333/gdctepg.xml"
MULTICAST_PROXY_BASE_URL = "http://10.10.10.253:33334"
RTP2HTTPD_RTSP_BASE_URL = "http://10.10.10.253:55140"
RTP2HTTPD_RTP_BASE_URL = "http://10.10.10.253:55140"
```

### 广东移动

编辑 `gdcmiptv.py` 顶部配置：

```python
USER_ID = ""
MAC = ""
STB_ID = ""
STB_TYPE = ""
STB_IP = ""
AREA_CODE = ""
SOFTWARE_VERSION = ""
```

如果使用华为 RedirectPlay 回看，还需要填写：

```python
HUAWEI_AUTH_PASSWORD = ""
```

### 上海电信

编辑 `shctiptv.py` 顶部配置：

```python
DEFAULT_USER_ID = ""
DEFAULT_SN = ""
DEFAULT_MAC = ""
DEFAULT_AUTH_HOST = "222.68.208.73:7001"
DEFAULT_UDPXY = "192.168.50.1:333"
DEFAULT_RTP2HTTPD_URL = "http://192.168.50.2:5140"
DEFAULT_EPG_URL = "http://192.168.50.2:3333/shctepg.xml"
```

上海脚本也保留命令行参数，例如：

```bash
python3 shctiptv.py --user-id '你的账号@etv1' --sn '你的SN' --mac '你的MAC'
```

## 定时任务示例

```cron
10 4 * * * cd /opt/iptv && /usr/bin/python3 gdctiptv.py >/tmp/gdctiptv.log 2>&1
20 4 * * * cd /opt/iptv && /usr/bin/python3 gdcmiptv.py >/tmp/gdcmiptv.log 2>&1
30 4 * * * cd /opt/iptv && /usr/bin/python3 shctiptv.py >/tmp/shctiptv.log 2>&1
```
## 参考项目
1、https://github.com/yujincheng08/rust-iptv-proxy
2、https://github.com/melody0709/cmcc_iptv_auto_py
3、https://github.com/denymz/sh-tel-iptv-spider


## 发布到 GitHub

发布公开仓库前，保持脚本里的账号、MAC、SN、Token 等字段为空或占位值，不要提交本地生成的 M3U、XML、缓存和抓包导出文件。

```bash
git add README.md .gitignore gdctiptv.py gdcmiptv.py shctiptv.py
git commit -m "Initial IPTV spider scripts"
git branch -M main
git remote add origin https://github.com/<your-name>/<repo-name>.git
git push -u origin main
```
