cmake_minimum_required(VERSION 3.16)
project(tuning_V2.3 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTOUIC ON)
set(CMAKE_AUTORCC ON)

# Find Qt5
find_package(Qt5 REQUIRED COMPONENTS Core Gui Widgets)

# Find OpenSSL
find_package(OpenSSL REQUIRED)

# Source files
set(SOURCES
    main.cpp
    mainwindow.cpp
)
set(HEADERS
    mainwindow.h
    hwhandler.h
)
set(UI_FILES
    mainwindow.ui
)
set(RESOURCE_FILES
    resources.qrc
)

# Create executable
add_executable(tuning_V2.3
    ${SOURCES}
    ${HEADERS}
    ${UI_FILES}
    ${RESOURCE_FILES}
)

# Include directories
target_include_directories(tuning_V2.3 PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${OPENSSL_INCLUDE_DIR}
    /usr/include
)

# Link Qt5
target_link_libraries(tuning_V2.3 PRIVATE
    Qt5::Core
    Qt5::Gui
    Qt5::Widgets
)

# Link OpenSSL
target_link_libraries(tuning_V2.3 PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
)

# Link hwhandler static library
target_link_libraries(tuning_V2.3 PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/libhwhandler.a
)

install(TARGETS tuning_V2.3 DESTINATION /opt/tuning_V2.3/bin)
