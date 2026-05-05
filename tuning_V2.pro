QT       += core gui widgets

greaterThan(QT_MAJOR_VERSION, 4): QT += widgets

CONFIG += c++17

TARGET = pyTunning
TEMPLATE = app

SOURCES += \
    main.cpp \
    mainwindow.cpp

HEADERS += \
    hwhandler.h \
    mainwindow.h

FORMS += \
    mainwindow.ui

RESOURCES += \
    resources.qrc

# Deployment
qnx: target.path = /tmp/$${TARGET}/bin
else: unix: target.path = /opt/$${TARGET}/bin
!isEmpty(target.path): INSTALLS += target

# ✅ Link your custom library (ONLY ONCE)
LIBS += -L$$PWD -lhwhandler

# Include path
INCLUDEPATH += $$PWD
DEPENDPATH += $$PWD

# Copy python script to deployment target
pythonfiles.files = python/tuning_mainwindow.py
pythonfiles.path = /opt/$${TARGET}/python
INSTALLS += pythonfiles

DISTFILES +=



win32:CONFIG(release, debug|release): LIBS += -L$$PWD/./release/ -lhwhandler
else:win32:CONFIG(debug, debug|release): LIBS += -L$$PWD/./debug/ -lhwhandler
else:unix: LIBS += -L$$PWD/./ -lhwhandler

INCLUDEPATH += $$PWD/.
DEPENDPATH += $$PWD/.
