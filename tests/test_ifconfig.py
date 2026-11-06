from mirrorway.discovery import parse_ifconfig

SAMPLE = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 3c:22:fb:00:00:01
\tinet6 fe80::1c%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.168.1.106 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
en5: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.0.0.9 netmask 0xffffff00 broadcast 10.0.0.255
awdl0: flags=8943<UP,BROADCAST,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::2%awdl0 prefixlen 64 scopeid 0xc
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 192.168.144.141 --> 192.168.144.141 netmask 0xffffff00
"""


def test_parse_ifconfig_prefers_wifi_over_vpn():
    res = parse_ifconfig(SAMPLE)
    assert [(d["name"], d["ip"], d["kind"]) for d in res] == [
        ("en0", "192.168.1.106", "lan"),
        ("utun4", "192.168.144.141", "vpn"),
    ]
    assert res[0]["broadcast"] == "192.168.1.255" and res[1]["broadcast"] is None
