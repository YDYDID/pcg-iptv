import os
import re

def clean_and_generate_m3u():
    # 1. 模拟抓取或读取你原本生成的原始电信 M3U 数据（这里以读取本地原始备份为例，请根据你实际的抓取逻辑调整）
    raw_m3u_path = "/data/iptv_raw.m3u"
    
    if not os.path.exists(raw_m3u_path):
        print(f"未找到原始 M3U 文件: {raw_m3u_path}，跳过清洗。")
        return

    with open(raw_m3u_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    srcbox_lines = ["#EXTM3U\n"]
    televizo_lines = ["#EXTM3U\n"]

    current_extinf = ""
    
    # 填入你之前配置的群晖反向代理环境变量，若无则使用默认值
    domain_port = os.getenv("DOMAIN_PORT", "你的群晖域名:5位端口")
    token = os.getenv("R2H_TOKEN", "你的16位Hex密码")

    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith("#EXTINF"):
            # 提取原始标签：fcc, fcc-type, group-title, tvg-id
            fcc = re.search(r'fcc="([^"]+)"', line)
            fcc_type = re.search(r'fcc-type="([^"]+)"', line)
            group = re.search(r'group-title="([^"]+)"', line)
            tvg_id = re.search(r'tvg-id="([^"]+)"', line)
            
            fcc_str = f' fcc="{fcc.group(1)}"' if fcc else ''
            fcc_type_str = f' fcc-type="{fcc_type.group(1)}"' if fcc_type else ''
            group_str = f' group-title="{group.group(1)}"' if group else ''
            tvg_id_str = f' tvg-id="{tvg_id.group(1)}"' if tvg_id else ' tvg-id="unknown"'
            
            # 提取频道名称（最后一项逗号后面的文本）
            ch_name = line.split(",")[-1] if "," in line else "未知频道"

            # 核心提取：提取原始未污染的电信回看 RTSP 核心路径（假设原始路径包含 /PLTV/）
            # 例如原始：rtsp://183.59.x.x/PLTV/88888888/111/index.m3u8
            rtsp_match = re.search(r'rtsp://([^"\s]+)', line)
            if rtsp_match:
                rtsp_path = rtsp_match.group(1)
                # 剔除原始可能存在的自带问号参数，只保留纯净路径
                rtsp_path = rtsp_path.split("?")[0]
            else:
                rtsp_path = "183.59.156.166/PLTV/88888888/default/index.m3u8"

            # 【核心组装】
            # 将私有非标准属性全部强行挪到 #EXTINF 最前端，腾空尾部，理顺唯一的问号与和号
            base_extinf = f'#EXTINF:-1{tvg_id_str}{fcc_str}{fcc_type_str}{group_str} catchup="default"'
            
            # 生成 SrcBox 专属行（大括号使用 utc 语法，并加 .000 毫秒）
            srcbox_catchup = f' catchup-source="https://{domain_port}/rtsp/{rtsp_path}?playseek={{utc:YmdHMS}}.000-{{utcend:YmdHMS}}.000&token={token}"'
            srcbox_lines.append(f"{base_extinf}{srcbox_catchup} ,{ch_name}\n")
            
            # 生成 Televizo / TiviMate 专属行（使用 $start 语法，并加 .000 毫秒）
            televizo_catchup = f' catchup-source="https://{domain_port}/rtsp/{rtsp_path}?playseek=${{start}}.000-${{end}}.000&token={token}"'
            televizo_lines.append(f"{base_extinf}{televizo_catchup} ,{ch_name}\n")

        elif line.startswith("http") or line.startswith("rtp") or line.startswith("udp"):
            # 如果是直播流地址，直接将其转换为经过群晖反向代理的组播/单播 HTTP 地址
            # 假设原始是 udp://239.77.x.x:端口，转换为代理形式
            if "239." in line:
                ip_port = line.split("//")[-1]
                proxy_live_url = f"https://{domain_port}/udp/{ip_port}"
            else:
                proxy_live_url = line
                
            srcbox_lines.append(f"{proxy_live_url}\n")
            televizo_lines.append(f"{proxy_live_url}\n")

    # 4. 写入成品文件到群晖挂载的 data 目录
    with open("/data/srcbox.m3u", "w", encoding="utf-8") as f:
        f.writelines(srcbox_lines)
    with open("/data/televizo.m3u", "w", encoding="utf-8") as f:
        f.writelines(televizo_lines)
        
    # 同时生成一份不带高级标签的纯净列表给 rtp2httpd 作为上游基准使用
    with open("/data/rtp_upstream.m3u", "w", encoding="utf-8") as f:
        f.writelines(televizo_lines)

    print("🎉 广州从化电信 M3U 矩阵列表清洗并分发成功！")

if __name__ == "__main__":
    clean_and_generate_m3u()
