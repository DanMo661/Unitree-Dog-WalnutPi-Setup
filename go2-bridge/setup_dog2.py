# -*- coding: utf-8 -*-
"""板2号狗(克隆镜像)一键对齐: DT-pwm12 + WiFi(静态192.168.8.51) + 代码 + 页面.
在板子 eth(123.50) 可达时于 PC 上运行: python setup_dog2.py"""
import paramiko

IP_ETH = "192.168.123.50"
WIFI_IP = "192.168.8.51"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(IP_ETH, username="pi", password="pi", timeout=15)


def run(cmd, t=90):
    _, o, e = c.exec_command(cmd, timeout=t)
    return o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")


def put(local_text, remote):
    sftp = c.open_sftp()
    with sftp.open(remote, "w") as f:
        f.write(local_text)
    sftp.close()


# ---- 1. 设备树: 只删 pwm3/4 pinctrl(=板1同款 -pwm12) ----
sftp = c.open_sftp()
sftp.put("dtb_free_pwm_pins.py", "/home/pi/go2-bridge/dtb_free_pwm_pins.py")
sftp.close()
print(run("echo pi | sudo -S python3 /home/pi/go2-bridge/dtb_free_pwm_pins.py "
          "/boot/sun50i-h616-walnutpi-1b.dtb /boot/sun50i-h616-walnutpi-1b-pwm12.dtb 34"))
print(run("echo pi | sudo -S python3 /home/pi/go2-bridge/dtb_free_pwm_pins.py "
          "/boot/sun50i-h616-walnutpi-1b-emmc.dtb /boot/sun50i-h616-walnutpi-1b-emmc-pwm12.dtb 34"))
print(run("echo pi | sudo -S sh -c 'cp /boot/config.txt /boot/config.txt.bak_dog2; "
          "sed -i \"s/^fdtfile=sun50i-h616-walnutpi-1b$/fdtfile=sun50i-h616-walnutpi-1b-pwm12/; "
          "s/^fdtfile_emmc=sun50i-h616-walnutpi-1b-emmc$/fdtfile_emmc=sun50i-h616-walnutpi-1b-emmc-pwm12/\" "
          "/boot/config.txt; grep fdtfile /boot/config.txt'"))

# ---- 2. WiFi: netplan 静态IP(板2=.51) + wpa 凭据(引号包裹psk) ----
yaml = """network:
  version: 2
  wifis:
    wlan0:
      dhcp4: false
      addresses: [%s/24]
      routes:
        - to: default
          via: 192.168.8.1
      nameservers:
        addresses: [192.168.8.1, 223.5.5.5]
      access-points:
        \"尧祐智能\":
          password: <HOME_WIFI_PSK>
""" % WIFI_IP
conf = """ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1
country=CN

network={
    ssid=\"尧祐智能\"
    psk=\"<HOME_WIFI_PSK>\"
    key_mgmt=WPA-PSK
}
"""
put(yaml, "/tmp/40-wifi.yaml")
put(conf, "/tmp/wpa-wlan0.conf")
print(run("echo pi | sudo -S sh -c 'mv /tmp/40-wifi.yaml /etc/netplan/40-wifi.yaml; "
          "chmod 600 /etc/netplan/40-wifi.yaml; "
          "mv /tmp/wpa-wlan0.conf /etc/wpa_supplicant/wpa_supplicant-wlan0.conf; "
          "chmod 600 /etc/wpa_supplicant/wpa_supplicant-wlan0.conf; "
          "netplan generate && echo netplan语法OK; netplan apply; "
          "systemctl disable --now wpa_supplicant 2>&1 | tail -1; "
          "rm -f /run/wpa_supplicant/wlan0 /run/wpa_supplicant/p2p-dev-wlan0; "
          "systemctl enable --now wpa_supplicant@wlan0 2>&1 | tail -1; sleep 10; "
          "systemctl is-active wpa_supplicant@wlan0; iw dev wlan0 link | head -3'"))
print("=== 完成, 等部署代码后重启 ===")
c.close()
