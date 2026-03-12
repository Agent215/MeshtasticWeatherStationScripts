import machine
import network
from machine import Pin, UART

from weather_bridge.adapters import MicroPythonUdp, MicroPythonUart, MicroPythonWifi, StdClock
from weather_bridge.diagnostics import Diagnostics
from weather_bridge.runtime import BridgeConfig, BridgeRuntime, FaultInjector

WIFI_SSID = "YOUR_WIFI_NAME"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
UDP_PORT = 50222
UART_BAUD = 115200
UART_TX_PIN = 0
UART_RX_PIN = 1


def build_runtime():
    uart_hw = UART(0, baudrate=UART_BAUD, tx=Pin(UART_TX_PIN), rx=Pin(UART_RX_PIN))
    config = BridgeConfig(wifi_ssid=WIFI_SSID, wifi_password=WIFI_PASSWORD)
    runtime = BridgeRuntime(
        config=config,
        wifi=MicroPythonWifi(network),
        udp=MicroPythonUdp(UDP_PORT, timeout=1.0),
        uart=MicroPythonUart(uart_hw),
        clock=StdClock(),
        diagnostics=Diagnostics(),
        injector=FaultInjector(),
    )
    return runtime


def main():
    runtime = build_runtime()
    runtime.run_forever(reset_callback=machine.reset)


main()
