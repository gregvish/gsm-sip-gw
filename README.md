# Quectel EG25 GSM modem to SIP Call Gateway Bot
GSM call forwarding to SIP. A bot that forwards calls (or SMS) from a Quectel EG25 LTE modem to a SIP URI

Required setup:
  * A Quectel EG25-G based modem. For example, [EG25-G USB modem from Aliexpress](https://www.aliexpress.com/item/4000140639655.html?spm=a2g0s.9042311.0.0.25e94c4dCiFyRj) (Separate IPX antenna required)
    * This is the same modem module that is used on the PinePhone (https://www.pine64.org/pinephone/) and in the Librem 5 design (https://puri.sm/products/librem-5/)
    * This project uses an open source **custom firmware** for the modem: https://github.com/Biktorgj/pinephone_modem_sdk/ (With the EN_USBAUD feature enabled)

# Prerequisites

This uses docker, and also assumes a proper setup of the hardware on the host environment. Essentially, the following udev rules should be placed (for examle) into `/etc/udev/rules.d/89-lte-modem.rules` on the host machine:
```
ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0125", ENV{PULSE_IGNORE}="1"
DRIVERS=="usb", KERNEL=="*cdc-wdm*", GROUP="dialout"
```
The Vendor and Product IDs here match the Quectel modem. This is necessary for the USB sound-card exposed by the modem to the host to be available for this software, and for permissions to be matching (`dialout` group, precisely).
Running `cat /proc/asound/cards` should show something like the following with regards to the Quectel modem soundcard:
```
$ cat /proc/asound/cards
...
 2 [Module         ]: USB-Audio - LTE Module
                      Quectel, Incorporated LTE Module at usb-0000:00:14.0-4.1, high speed
```
The name of the soundcard is `Module`.
Apart from this, the AT command port of the modem should be found, on a path like `/dev/ttyUSB2`, and the QMI port under `/dev/cdc-wdm0`. Both need to be usable by the `dialout` group on your host.

# Building and running
```
./doit.sh --sip_dest <SIP-URI> --modem_tty /dev/ttyUSB2 --modem_dev /dev/cdc-wdm0
```
This builds the docker image, and runs it as daemon that also survives reboots. The ouput can be seen using `docker logs -f gsm-sip-gw-container`
