# zmk-renode-test-transport CMakeLists.txt
#
# Adds src/renode_uart_transport.c to the app image only when
# CONFIG_ZMK_RENODE_STUDIO_UART_TRANSPORT=y (opt-in, default n — see Kconfig
# for why this exists and why it must never be enabled for real hardware).

if(CONFIG_ZMK_RENODE_STUDIO_UART_TRANSPORT)
    target_sources(app PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src/renode_uart_transport.c)
endif()
