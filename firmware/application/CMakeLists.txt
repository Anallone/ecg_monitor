# application/CMakeLists.txt
#
# Module registration for the business modules under application/.
#
# Each module directory carries a register.cmake whose content is a single line:
#
#     app_register(<module_name>)
#
# This file is included TWICE on purpose:
#   1) from firmware/CMakeLists.txt, before project(), so that the modules are
#      contributed to EXTRA_COMPONENT_DIRS and discovered as IDF components;
#   2) from main/CMakeLists.txt, to collect APPLICATION_MODULES into main's
#      (PRIV_)REQUIRES so every module gets linked in.
#
# NOTE: app_register MUST be a macro, not a function. A CMake function gets its
# own variable scope, so APPLICATION_MODULES / EXTRA_COMPONENT_DIRS would be
# lost when it returns.
#
# NOTE: CMAKE_CURRENT_LIST_DIR is used deliberately. When include() processes a
# module's register.cmake, that variable points at the directory of register.cmake
# itself, i.e. the module directory -- which is exactly what the
# IS_DIRECTORY() check below needs in order to support both flat modules
# (application/<mod>/) and container modules (application/<group>/<mod>/).

macro(app_register name)
    list(APPEND APPLICATION_MODULES ${name})
    if(IS_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/${name}")
        # container module: the component lives one level down
        list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/${name}")
    else()
        # flat module: register the directory that holds register.cmake
        list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}")
    endif()
endmacro()

# Conditional registration, driven by Kconfig options (CONFIG_*):
#     app_register_if(CONFIG_ENABLE_FOO foo)
macro(app_register_if condition name)
    if(${condition})
        app_register(${name})
    endif()
endmacro()

# Discover every module's register.cmake and let it register itself.
#
# In normal (project) mode we ask for CONFIGURE_DEPENDS so that adding or
# removing an application/<mod>/register.cmake is noticed without a manual
# `idf.py reconfigure`. ESP-IDF also includes this file in *script* mode
# (tools/cmake/scripts/component_get_requirements.cmake), where
# CONFIGURE_DEPENDS is rejected -- hence the guard.
if(DEFINED CMAKE_SCRIPT_MODE_FILE)
    file(GLOB _APP_REGISTER_FILES "${CMAKE_CURRENT_LIST_DIR}/*/register.cmake")
else()
    file(GLOB _APP_REGISTER_FILES CONFIGURE_DEPENDS
         "${CMAKE_CURRENT_LIST_DIR}/*/register.cmake")
endif()
foreach(_F ${_APP_REGISTER_FILES})
    include(${_F})
endforeach()
