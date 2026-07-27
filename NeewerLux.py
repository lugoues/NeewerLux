#!/usr/bin/python3
############################################################
## NeewerLux
## A NeewerLite-Python Extension
############################################################
## Based on NeewerLite-Python ver. 0.12d by Zach Glenwright
## > https://github.com/taburineagle/NeewerLite-Python/ <
##
## Which is based on the NeewerLite project by Xu Lian (@keefo)
## > https://github.com/keefo/NeewerLite <
############################################################
## A cross-platform Python service using the bleak library to control
## Neewer brand lights via
## Bluetooth on multiple platforms -
##          Windows, Linux/Ubuntu, MacOS and RPi
############################################################

NEEWERLUX_VERSION = "1.2.0"
NEEWERLUX_REPO_URL = "https://github.com/poizenjam/NeewerLux/"
NEEWERLUX_RELEASES_API = "https://api.github.com/repos/poizenjam/NeewerLux/releases/latest"

import os
import sys

# When launched via pythonw.exe or .pyw, stdout/stderr are None.
# Redirect to devnull so print() calls don't crash.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

import math # used for calculating the RGB values of color temperatures
import json # used for animation files and HTTP batch/animation APIs
import shutil # used to seed the default preset file on first run
import tempfile
import faulthandler
import warnings
faulthandler.enable() # print traceback on segfault/C++ crashes
warnings.filterwarnings("ignore", category=FutureWarning, message=".*BLEDevice.rssi.*")  # suppress Bleak deprecation noise

import argparse
import platform # used to determine which OS we're using for MAC address/GUID listing

import asyncio
import threading
import time

from datetime import datetime

# IMPORT BLEAK (this is the library that allows the program to communicate with the lights) - THIS IS NECESSARY!
try:
    from bleak import BleakScanner, BleakClient
except ModuleNotFoundError as e:
    print(" ===== CAN NOT FIND BLEAK LIBRARY =====")
    print(" You need the bleak Python package installed to use NeewerLux.")
    print(" Bleak is the library that connects the program to Bluetooth devices.")
    print(" Please install the Bleak package first before running NeewerLux.")
    print()
    print(" To install Bleak, run either pip or pip3 from the command line:")
    print("    pip install bleak")
    print("    pip3 install bleak")
    print()
    print(" Or visit this website for more information:")
    print("    https://pypi.org/project/bleak/")
    sys.exit(1) # you can't use the program itself without Bleak, so kill the program if we don't have it

# IMPORT THE WINDOWS LIBRARY (needed for older bleak versions on Windows; newer bleak handles this internally)
if platform.system() == "Windows":
    try:
        from winrt import _winrt
        _winrt.uninit_apartment()
    except Exception:
        try:
            # Newer winrt package structure (winrt-runtime)
            import winrt.system
        except Exception:
            pass # modern bleak (0.20+) handles COM apartment threading internally

# IMPORT THE WEB DASHBOARD, which is the only user interface NeewerLux has.
from neewerlux_webui import getWebDashboardHTML

# SET WINDOWS APP ID SO THE TASKBAR SHOWS OUR ICON INSTEAD OF PYTHON'S
try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("poizenjam.NeewerLux." + NEEWERLUX_VERSION)
except Exception:
    pass  # not Windows

# CONSOLE MANAGEMENT
# When launched via pythonw.exe (or NeewerLux.bat), there is no console, these are no-ops.
# When launched via python.exe directly, SW_HIDE works on legacy conhost but NOT on
# Windows Terminal (which intercepts and downgrades to minimize).
_hasConsole = False
try:
    import ctypes
    import ctypes.wintypes as wintypes
    ctypes.windll.kernel32.GetConsoleWindow.restype = wintypes.HWND
    ctypes.windll.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    ctypes.windll.user32.ShowWindow.restype = wintypes.BOOL
    _consoleHwnd = ctypes.windll.kernel32.GetConsoleWindow()
    _hasConsole = bool(_consoleHwnd)
except Exception:
    _consoleHwnd = 0

def hideConsoleWindow():
    if _consoleHwnd:
        try:
            ctypes.windll.user32.ShowWindow(_consoleHwnd, 0)  # SW_HIDE
        except Exception:
            pass

def showConsoleWindow():
    if _consoleHwnd:
        try:
            ctypes.windll.user32.ShowWindow(_consoleHwnd, 5)  # SW_SHOW
        except Exception:
            pass

# IMPORT THE HTTP SERVER
try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import urllib.parse # parsing custom light names in the HTTP server
except Exception as e:
    pass # if there are any HTTP errors, don't do anything yet

def _resource_path(filename):
    """Absolute path to a bundled resource. PyInstaller unpacks to _MEIPASS; source runs use the script dir."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), filename)

# Are we running as a frozen PyInstaller EXE?
_isFrozenExe = getattr(sys, 'frozen', False)

CCTSlider = -1 # the current slider moved in the CCT window - 1 - Brightness / 2 - Hue / -1 - Both Brightness and Hue
sendValue = [120, 135, 2, 20, 56, 157] # an array to hold the values to be sent to the light - the default is CCT / 5600K / 20%
lastAnimButtonPressed = 1 # which animation button you clicked last - if none, then it defaults to 1 (the police sirens)
lastSelection = [] # the current light selection (this is for snapshot preset entering/leaving buttons)
lastSortingField = -1 # the last field used for sorting purposes

availableLights = [] # the list of Neewer lights currently available to control
# List Subitems (for ^^^^^^):
# [0] - Bleak Scan Object (can use .name / .address to get specifics; RSSI stored separately at index [9])
# [1] - Bleak Connection (the actual Bluetooth connection to the light itself)
# [2] - Custom Name for Light (string)
# [3] - Last Used Parameters (list)
# [4] - The range of color temperatures to use in CCT mode (list, min, max) <- changed in 0.12
# [5] - Whether or not to send Brightness and Hue independently for old lights (boolean)
# [6] - Whether or not this light has been manually turned ON/OFF (boolean)
# [7] - The Power and Channel data returned for this light (list)
# [8] - Preferred ID for sorting (int)
# [9] - Last known RSSI value in dBm (int or str "?")

# Light Preset ***Default*** Settings (for sections below):
# NOTE: The list is 0-based, so the preset itself is +1 from the subitem
# [0] - [CCT mode] - 5600K / 20%
# [1] - [CCT mode] - 3200K / 20%
# [2] - [CCT mode] - 5600K / 0% (lights are on, but set to 0% brightness)
# [3] - [HSI mode] - 0° hue / 100% saturation / 20% intensity (RED)
# [4] - [HSI mode] - 240° hue / 100% saturation / 20% intensity (BLUE)
# [5] - [HSI mode] - 120° hue / 100% saturation / 20% intensity (GREEN)
# [6] - [HSI mode] - 300° hue / 100% saturation / 20% intensity (PURPLE)
# [7] - [HSI mode] - 160° hue / 100% saturation / 20% intensity (CYAN)

# The 8 factory default presets (always available as a fallback)
factoryDefaultPresets = [
    [[-1, [5, 20, 56]]],
    [[-1, [5, 20, 32]]],
    [[-1, [5, 0, 56]]],
    [[-1, [4, 20, 0, 100]]],
    [[-1, [4, 20, 240, 100]]],
    [[-1, [4, 20, 120, 100]]],
    [[-1, [4, 20, 300, 100]]],
    [[-1, [4, 20, 160, 100]]]    
    ]

numOfPresets = 8 # how many presets are active (can grow beyond 8)
presetNames = {} # dict of {preset_index: "custom name"} for user-assigned names

# ============================================================================
# CUSTOM ANIMATION SYSTEM
# ============================================================================
animationRunning = False       # whether an animation is currently playing
animationStopFlag = False      # set True to request the animation to stop
_animChainStop = False         # set when stopping to chain into another animation (skips revert)
currentAnimationName = ""      # name of the currently playing animation
savedAnimations = {}           # dict of {name: animation_dict} loaded from disk
animationsDir = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs" + os.sep + "animations"
lightAliases = {}  # {MAC: {"id": int, "name": str}}, built from per-light prefs sidecar files
workerWakeEvent = threading.Event()  # signaled by animation thread to wake the worker immediately
animParallelWrites = True  # send BLE commands to all lights simultaneously during animations
animRevertOnFinish = True  # revert lights to pre-animation state when a non-looping animation finishes
preAnimationStates = {}    # {light_index: byte_list} saved before animation starts

# HTTP SERVER (GUI-LAUNCHED) MANAGEMENT
httpServerInstance = None   # ThreadingHTTPServer object (when running from GUI)
httpServerThread = None     # daemon thread running serve_forever()
httpServerRunning = False   # flag for UI state

def getDefaultPreset(index):
    """Return the factory default for a given preset index, cycling through the 8 built-in defaults."""
    return factoryDefaultPresets[index % len(factoryDefaultPresets)]

def buildDefaultPresets(count):
    """Build a list of default presets of the given length."""
    return [getDefaultPreset(i) for i in range(count)]

# The list of **default** light presets for restoring and checking against
defaultLightPresets = buildDefaultPresets(numOfPresets)

# A list of preset mode settings - custom file will overwrite
customLightPresets = buildDefaultPresets(numOfPresets)

threadAction = "" # the current action to take from the thread
asyncioEventLoop = None # the current asyncio loop

setLightUUID = "69400002-B5A3-F393-E0A9-E50E24DCCA99" # the UUID to send information to the light
notifyLightUUID = "69400003-B5A3-F393-E0A9-E50E24DCCA99" # the UUID for notify callbacks from the light

receivedData = "" # the data received from the Notify characteristic

# SET FROM THE PREFERENCES FILE ON LAUNCH
findLightsOnStartup = True # whether or not to look for lights when the program starts
autoConnectToLights = True # whether or not to auto-connect to lights after finding them
printDebug = True # show debug messages in the console for all of the program's events
maxNumOfAttempts = 6 # the maximum attempts the program will attempt an action before erroring out
rememberLightsOnExit = False # whether or not to save the currently set light settings (mode/hue/brightness/etc.) when quitting out
rememberPresetsOnExit = True # whether or not to save the custom preset list when quitting out
livePreview = True # whether sliders send values in real-time or require clicking Apply
hideConsoleOnLaunch = False # whether to auto-hide the console window on GUI startup
minimizeToTrayOnClose = True # whether closing the window minimizes to tray (True) or quits (False)
httpAutoStart = False # whether to automatically start the HTTP server on launch
httpPort = 8080 # port the HTTP server listens on
cctFallbackMode = "convert" # how to handle HSI/ANM commands sent to CCT-only lights: "ignore" or "convert"
enableLogTab = True # whether to show and populate the Log tab
logToFile = False # whether to also write log entries to a file
globalCCTMin = 3200 # global default minimum color temperature (K)
globalCCTMax = 5600 # global default maximum color temperature (K)
autoReconnectOnDisconnect = True # whether or not to automatically try reconnecting to lights that disconnect (e.g. after sleep/wake)
acceptable_HTTP_IPs = [] # the acceptable IPs for the HTTP server, set on launch by prefs file
customKeys = [] # custom keymappings for keyboard shortcuts, set on launch by the prefs file
whiteListedMACs = [] # whitelisted list of MAC addresses to add to NeewerLux
enableTabsOnLaunch = False # whether or not to enable tabs on startup (even with no lights connected)

lockFile = tempfile.gettempdir() + os.sep + "NeewerLux.lock"
anotherInstance = False # whether or not we're using a new instance (for the Singleton check)
globalPrefsFile = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs" + os.sep + "NeewerLux.prefs" # the global preferences file for saving/loading
customLightPresetsFile = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs" + os.sep + "customLights.prefs"
# Shipped template used to seed customLightPresetsFile on first run. The live file is
# user state and is not tracked in the repository, so the defaults live here instead.
defaultLightPresetsFile = customLightPresetsFile + ".default"
# Records that seeding already happened, so a later deliberate reset or delete is not
# undone by copying the shipped presets back in.
presetsSeededMarkerFile = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs" + os.sep + ".presets_seeded"
geometryPrefsFile = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs" + os.sep + "NeewerLux.geometry"
logFilePath = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs" + os.sep + "NeewerLux.log"

# FILE LOCKING FOR SINGLE INSTANCE
def singleInstanceLock():
    global anotherInstance

    try:
        lf = os.open(lockFile, os.O_WRONLY | os.O_CREAT | os.O_EXCL) # try to get a file spec to lock the "running" instance

        with os.fdopen(lf, 'w') as lockfile:
            lockfile.write(str(os.getpid())) # write the PID of the current running process to the temporary lockfile
    except (IOError, OSError): # if we had an error acquiring the file descriptor, the file most likely already exists.
        # CHECK IF THE EXISTING LOCK FILE IS STALE (process no longer running)
        try:
            with open(lockFile, 'r') as f:
                oldPid = int(f.read().strip())

            # Check if the process with that PID is still alive
            pidAlive = False
            if oldPid == os.getpid():
                pidAlive = False  # it's our own stale lock from a crash
            elif sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # Use PROCESS_QUERY_LIMITED_INFORMATION to check if process is truly alive
                handle = kernel32.OpenProcess(0x1000, False, oldPid)
                if handle:
                    # Check exit code, STILL_ACTIVE (259) means genuinely running
                    exitCode = ctypes.c_ulong()
                    if kernel32.GetExitCodeProcess(handle, ctypes.byref(exitCode)):
                        pidAlive = (exitCode.value == 259)  # 259 = STILL_ACTIVE
                    kernel32.CloseHandle(handle)
            else:
                try:
                    os.kill(oldPid, 0) # signal 0 doesn't kill, just checks if PID exists
                    pidAlive = True
                except OSError:
                    pidAlive = False

            if pidAlive:
                anotherInstance = True # genuinely another instance running
            else:
                # Stale lock file, remove it and create a fresh one
                print("Found stale lock file (PID " + str(oldPid) + " is no longer running). Cleaning up...")
                os.remove(lockFile)
                lf = os.open(lockFile, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(lf, 'w') as lockfile:
                    lockfile.write(str(os.getpid()))
        except (ValueError, IOError, OSError):
            # Lock file exists but can't be read or PID is invalid, remove and recreate
            try:
                os.remove(lockFile)
                lf = os.open(lockFile, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(lf, 'w') as lockfile:
                    lockfile.write(str(os.getpid()))
            except (IOError, OSError):
                anotherInstance = True # truly can't acquire the lock
    
def singleInstanceUnlockandQuit(exitCode):
    # Flush any buffered log entries before exiting
    if _logBuffer:
        try:
            with open(logFilePath, "a", encoding="utf-8") as f:
                f.write("\n".join(_logBuffer) + "\n")
            _logBuffer.clear()
        except Exception:
            pass

    try:
        os.remove(lockFile) # try to delete the lockfile on exit
    except FileNotFoundError: # if another process deleted it, then just error out
        printDebugString("Lockfile not found in temp directory, so we're going to skip deleting it!")

    sys.exit(exitCode) # quit out, with the specified exitCode
    # If sys.exit was somehow caught, force-kill the process
    os._exit(exitCode)

def doAnotherInstanceCheck():
    if anotherInstance == True: # if we're running a 2nd instance, but we shouldn't be
        print("You're already running another instance of NeewerLux.")
        print("Please close that copy first before opening a new one.")
        print()
        print("To force opening a new instance, add --force_instance to the command line.")
        sys.exit(1)

def savePresetsToFile():
    """Write the custom presets and their names to disk.

    Deletes the file when nothing differs from the factory presets, which is what
    makes a reset persist instead of being re-seeded on the next launch.
    """
    if not rememberPresetsOnExit:
        return

    try:
        customPresetsToWrite = ["numOfPresets=" + str(numOfPresets)]

        for i in range(numOfPresets):
            if customLightPresets[i] != defaultLightPresets[i]:
                customPresetsToWrite.append(customPresetToString(i))

        for idx, name in presetNames.items():
            if name:
                customPresetsToWrite.append("presetName" + str(idx) + "=" + name)

        if len(customPresetsToWrite) > 1 or numOfPresets != 8:
            createLightPrefsFolder()

            with open(customLightPresetsFile, mode="w", encoding="utf-8") as f:
                f.write("\n".join(customPresetsToWrite))

            printDebugString("Saved presets to " + customLightPresetsFile)
        elif os.path.exists(customLightPresetsFile):
            os.remove(customLightPresetsFile)
    except OSError as e:
        printDebugString("Error saving presets: " + str(e))

def setUpAsyncio():
    global asyncioEventLoop

    try:
        asyncioEventLoop = asyncio.get_running_loop()
    except RuntimeError:
        asyncioEventLoop = asyncio.new_event_loop()

    asyncio.set_event_loop(asyncioEventLoop)

# CALCULATE THE RGB VALUE OF COLOR TEMPERATURE
def convert_K_to_RGB(Ktemp):
    # Based on this script: https://gist.github.com/petrklus/b1f427accdf7438606a6
    # from @petrklus on GitHub (his source was from http://www.tannerhelland.com/4435/convert-temperature-rgb-algorithm-code/)

    tmp_internal = Ktemp / 100.0
    
    # red 
    if tmp_internal <= 66:
        red = 255
    else:
        tmp_red = 329.698727446 * math.pow(tmp_internal - 60, -0.1332047592)

        if tmp_red < 0:
            red = 0
        elif tmp_red > 255:
            red = 255
        else:
            red = tmp_red
    
    # green
    if tmp_internal <= 66:
        tmp_green = 99.4708025861 * math.log(tmp_internal) - 161.1195681661

        if tmp_green < 0:
            green = 0
        elif tmp_green > 255:
            green = 255
        else:
            green = tmp_green
    else:
        tmp_green = 288.1221695283 * math.pow(tmp_internal - 60, -0.0755148492)

        if tmp_green < 0:
            green = 0
        elif tmp_green > 255:
            green = 255
        else:
            green = tmp_green
    
    # blue
    if tmp_internal >= 66:
        blue = 255
    elif tmp_internal <= 19:
        blue = 0
    else:
        tmp_blue = 138.5177312231 * math.log(tmp_internal - 10) - 305.0447927307
        if tmp_blue < 0:
            blue = 0
        elif tmp_blue > 255:
            blue = 255
        else:
            blue = tmp_blue
    
    return int(red), int(green), int(blue) # return the integer value for each part of the RGB values for this step

def convert_HSI_to_RGB(h, s = 1, v = 1):
    # Taken from this StackOverflow page, which is an articulation of the colorsys code to
    # convert HSV values (not HSI, but close, as I'm keeping S and V locked to 1) to RGB:
    # https://stackoverflow.com/posts/26856771/revisions

    if s == 0.0: v*=255; return (v, v, v)
    i = int(h*6.) # XXX assume int() truncates!
    f = (h*6.)-i; p,q,t = int(255*(v*(1.-s))), int(255*(v*(1.-s*f))), int(255*(v*(1.-s*(1.-f)))); v*=255; i%=6
    if i == 0: return (v, t, p)
    if i == 1: return (q, v, p)
    if i == 2: return (p, v, t)
    if i == 3: return (p, q, v)
    if i == 4: return (t, p, v)
    if i == 5: return (v, p, q)

def saveLightPrefs(lightID, deleteFile = False): # save a sidecar file with the preferences for a specific light
    createLightPrefsFolder() # create the light_prefs folder if it doesn't exist

    # GET THE CUSTOM FILENAME FOR THIS FILE, NOTED FROM THE MAC ADDRESS OF THE CURRENT LIGHT
    exportFileName = availableLights[lightID][0].address.split(":") # take the colons out of the MAC address
    exportFileName = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs" + os.sep + "".join(exportFileName)

    if deleteFile == True:
        if os.path.exists(exportFileName):
            os.remove(exportFileName) # delete the old preferences file (if it exists)
    else:
        customName = availableLights[lightID][2] # the custom name for this light
        defaultSettings = getLightSpecs(availableLights[lightID][0].name)

        if defaultSettings[1] != availableLights[lightID][4]:
            customTempRange = str(availableLights[lightID][4][0]) + "," + str(availableLights[lightID][4][1]) # the color temperature range available
        else:
            customTempRange = "" # if the range is the same as the default range, then just leave this entry blank

        if defaultSettings[2] != availableLights[lightID][5]:
            onlyCCTMode = str(availableLights[lightID][5]) # whether or not the light can only use CCT mode
        else:
            onlyCCTMode = "" # if the CCT mode enable is the same as the default value, then just leave this entry blank

        exportString = customName + "|" + customTempRange + "|" + onlyCCTMode # the exported string, minus the light last set parameters

        if rememberLightsOnExit == True: # if we're supposed to remember the last settings, then add that to the Prefs file
            if len(availableLights[lightID][3]) > 0: # if we actually have a value stored for this light
                lastSettingsString = ",".join(map(str, availableLights[lightID][3])) # combine all the elements of the last set params
                exportString += "|" + lastSettingsString # add it to the exported string
            else: # if we don't have a value stored for this light (nothing has changed yet)
                exportString += "|" + "120,135,2,100,56,157" # then just give the default (CCT, 5600K, 100%) params
        else:
            exportString += "|" # empty lastSettings field so preferredID stays in correct position

        # Preferred ID (5th pipe field), 0 means auto/no preference
        preferredID = availableLights[lightID][8] if len(availableLights[lightID]) > 8 else 0
        exportString += "|" + str(preferredID)

        # WRITE THE PREFERENCES FILE
        with open(exportFileName, mode="w", encoding="utf-8") as prefsFileToWrite:
            prefsFileToWrite.write(exportString)

        if customName != "":
            printDebugString("Exported preferences for " + customName + " [" + availableLights[lightID][0].name + "] to " + exportFileName)
        else:
            printDebugString("Exported preferences for [" + availableLights[lightID][0].name + "] to " + exportFileName)

# WORKING WITH CUSTOM PRESETS
def customPresetInfoBuilder(numOfPreset, formatForHTTP = False):
    toolTipBuilder = [] # constructor for the tooltip
    numOfLights = len(customLightPresets[numOfPreset]) # the number of lights in this specific preset

    # Show the custom name if one is set
    name = presetNames.get(numOfPreset, "")
    if name:
        if formatForHTTP == False:
            toolTipBuilder.append("\"" + name + "\"")
        else:
            toolTipBuilder.append("<STRONG>\"" + name + "\"</STRONG>")

    if numOfLights == 1 and customLightPresets[numOfPreset][0][0] == -1: # we're looking at a global preset
        if formatForHTTP == False:
            toolTipBuilder.append("[GLOBAL PRESET]")
        else:
            toolTipBuilder.append("<STRONG>[GLOBAL PRESET]</STRONG>")
    else: # we're looking at a snapshot preset
        if formatForHTTP == False:
            toolTipBuilder.append("[SNAPSHOT PRESET]")
        else:
            toolTipBuilder.append("<STRONG>[SNAPSHOT PRESET]</STRONG>")

    toolTipBuilder.append("")

    for a in range(numOfLights): # write out a little description of each part of this preset
        if customLightPresets[numOfPreset][a][0] == -1:
            if formatForHTTP == False:
                toolTipBuilder.append(" FOR: ALL SELECTED LIGHTS") # this is a global preset, and it affects all *selected* lights
            else:
                toolTipBuilder.append(" FOR: ALL LIGHTS AVAILABLE") # this is a global preset, and it affects all lights
        else:
            currentLight = returnLightIndexesFromMacAddress(customLightPresets[numOfPreset][a][0]) # find the light in the current list

            if currentLight != []: # if we have a match, add it to the list of lights to highlight
                if availableLights[currentLight[0]][2] != "": # if the custom name is filled in
                    toolTipBuilder.append(" FOR: " + availableLights[currentLight[0]][2] + " [" + availableLights[currentLight[0]][0].name + "]")
                else:
                    toolTipBuilder.append(" FOR: " + availableLights[currentLight[0]][0].name)
            else:
                toolTipBuilder.append("FOR: ---LIGHT NOT AVAILABLE AT THE MOMENT---") # if the light is not found (yet), display that

            toolTipBuilder.append(" " + customLightPresets[numOfPreset][a][0] + "") # this is a snapshot preset, and this specific preset controls this light
                    
        if customLightPresets[numOfPreset][a][1][0] == 5:
            if formatForHTTP == False:
                toolTipBuilder.append(" > MODE: CCT / TEMP: " + str(customLightPresets[numOfPreset][a][1][2]) + "00K / BRIGHTNESS: " + str(customLightPresets[numOfPreset][a][1][1]) + "% < ")
            else:
                toolTipBuilder.append(" &gt; MODE: CCT / TEMP: " + str(customLightPresets[numOfPreset][a][1][2]) + "00K / BRIGHTNESS: " + str(customLightPresets[numOfPreset][a][1][1]) + "% &lt; ")
        elif customLightPresets[numOfPreset][a][1][0] == 4:
            if formatForHTTP == False:
                toolTipBuilder.append(" > MODE: HSI / H: " + str(customLightPresets[numOfPreset][a][1][2]) + "º / S: " + str(customLightPresets[numOfPreset][a][1][3]) + "% / I: " + str(customLightPresets[numOfPreset][a][1][1]) + "% < ")
            else: # if we're sending this string back for the HTTP server, then replace the degree with the HTML version
                toolTipBuilder.append(" &gt; MODE: HSI / H: " + str(customLightPresets[numOfPreset][a][1][2]) + "&#176; / S: " + str(customLightPresets[numOfPreset][a][1][3]) + "% / I: " + str(customLightPresets[numOfPreset][a][1][1]) + "% &lt; ")
        elif customLightPresets[numOfPreset][a][1][0] == 6:
            if formatForHTTP == False:
                toolTipBuilder.append(" > MODE: SCENE / ANIMATION: " + str(customLightPresets[numOfPreset][a][1][2]) + " / BRIGHTNESS: " + str(customLightPresets[numOfPreset][a][1][1]) + "% < ")
            else:
                toolTipBuilder.append(" &gt; MODE: SCENE / ANIMATION: " + str(customLightPresets[numOfPreset][a][1][2]) + " / BRIGHTNESS: " + str(customLightPresets[numOfPreset][a][1][1]) + "% &lt; ")
        else: # if we're set to turn the light off, show that here
            if formatForHTTP == False:
                toolTipBuilder.append(" > TURN THIS LIGHT OFF < ")
            else:
                toolTipBuilder.append(" &gt; TURN THIS LIGHT OFF &lt; ")

        if numOfLights > 1 and a < (numOfLights - 1): # if we have any more lights, then separate each one
            if formatForHTTP == False:
                toolTipBuilder.append("----------------------------")
            else:
                toolTipBuilder.append("")
            
    if formatForHTTP == False:
        return "\n".join(toolTipBuilder)
    else:
        return "<BR>".join(toolTipBuilder)

def recallCustomPreset(numOfPreset, updateGUI=True, loop=None):
    global availableLights
    global lastSelection
    global threadAction

    # If an animation is playing, stop it, user is recalling a preset
    if animationRunning:
        stopAnimation()
        try:
            pass # the GUI notification that lived here is gone
        except Exception:
            pass

    changedLights = [] # if a snapshot preset exists in this setting, log the lights that are to be changed here

    for a in range(len(customLightPresets[numOfPreset])): # check all the entries stored in this preset
        if customLightPresets[numOfPreset][0][0] == -1: # we're looking at a global preset, so set the light(s) up accordingly
            
            if updateGUI == True: # if we are in the GUI
                if True: # and no lights are selected in the light selector
                    time.sleep(0.2)
            
            if customLightPresets[numOfPreset][0][1][0] == 5: # the preset is in CCT mode
                p_colorMode = "CCT"
                p_brightness = customLightPresets[numOfPreset][0][1][1]
                p_temp = customLightPresets[numOfPreset][0][1][2]

                if updateGUI == True:
                    pass # the GUI notification that lived here is gone
                else:
                    computedValue = calculateByteString(True, colorMode=p_colorMode, brightness=p_brightness, temp=p_temp)
            elif customLightPresets[numOfPreset][0][1][0] == 4: # the preset is in HSI mode
                p_colorMode = "HSI"
                # Due to the way the custom presets store information (brightness is always first),
                # this section is broken up into H, S and I portions for readability
                p_hue = customLightPresets[numOfPreset][0][1][2]
                p_sat = customLightPresets[numOfPreset][0][1][3]
                p_int = customLightPresets[numOfPreset][0][1][1]

                if updateGUI == True:
                    pass # the GUI notification that lived here is gone
                else:
                    computedValue = calculateByteString(True, colorMode=p_colorMode, HSI_H=p_hue, HSI_S=p_sat, HSI_I=p_int)
            elif customLightPresets[numOfPreset][0][1][0] == 6: # the preset is in ANM/SCENE mode
                p_colorMode = "ANM"
                p_brightness = customLightPresets[numOfPreset][0][1][1]
                p_scene = customLightPresets[numOfPreset][0][1][2]

                if updateGUI == True:
                    pass # the GUI notification that lived here is gone
                else:
                    computedValue = calculateByteString(True, colorMode=p_colorMode, brightness=p_brightness, scene=p_scene)

            if updateGUI == False:
                for b in range(len(availableLights)):
                    changedLights.append(b) # add each light to changedLights
                    availableLights[b][3] = computedValue # set each light's "last" parameter to the computed value above
            else:
                # Presets always send immediately, force a send even if livePreview is off
                if not livePreview:
                    threadAction = "send"

        else: # we're looking at a snapshot preset, so see if any of those lights are available to change
            currentLight = returnLightIndexesFromMacAddress(customLightPresets[numOfPreset][a][0])

            if currentLight != []: # if we have a match
                # always refer to the light it found as currentLight[0]
                if customLightPresets[numOfPreset][a][1][0] == 5 or customLightPresets[numOfPreset][a][1][0] == 8: # the preset is in CCT mode
                    availableLights[currentLight[0]][3] = calculateByteString(True, colorMode="CCT",\
                                                            brightness=customLightPresets[numOfPreset][a][1][1],\
                                                            temp=customLightPresets[numOfPreset][a][1][2])

                    if customLightPresets[numOfPreset][a][1][0] == 8: # if we want to turn the light off, let the send system know this
                        availableLights[currentLight[0]][3][0] = 0
                elif customLightPresets[numOfPreset][a][1][0] == 4 or customLightPresets[numOfPreset][a][1][0] == 7: # the preset is in HSI mode
                    availableLights[currentLight[0]][3] = calculateByteString(True, colorMode="HSI",\
                                                            HSI_I=customLightPresets[numOfPreset][a][1][1],\
                                                            HSI_H=customLightPresets[numOfPreset][a][1][2],\
                                                            HSI_S=customLightPresets[numOfPreset][a][1][3])

                    if customLightPresets[numOfPreset][a][1][0] == 7: # if we want to turn the light off, let the send system know this
                        availableLights[currentLight[0]][3][0] = 0
                elif customLightPresets[numOfPreset][a][1][0] == 6 or customLightPresets[numOfPreset][a][1][0] == 9: # the preset is in ANM/SCENE mode
                    availableLights[currentLight[0]][3] = calculateByteString(True, colorMode="ANM",\
                                                            brightness=customLightPresets[numOfPreset][a][1][1],\
                                                            animation=customLightPresets[numOfPreset][a][1][2])
                    
                    if customLightPresets[numOfPreset][a][1][0] == 9: # if we want to turn the light off, let the send system know this
                        availableLights[currentLight[0]][3][0] = 0
                
                changedLights.append(currentLight[0])

    if changedLights != []:
        if updateGUI == True:
            lastSelection = [] # clear the last selection if you've clicked on a snapshot preset (which, if we're here, you did)


        # Always use the threadAction approach, this is safe from both GUI and HTTP threads
        threadAction = "send|" + "|".join(map(str, changedLights))

def saveCustomPreset(presetType, numOfPreset, selectedLights = []):
    global customLightPresets

    if presetType == "global":
        customLightPresets[numOfPreset] = [listBuilder(-1)]
    elif presetType == "snapshot":
        listConstructor = []
        
        if selectedLights == []: # add all the lights to the snapshot preset
            for a in range(len(availableLights)): 
                listConstructor.append(listBuilder(a))
        else: # add only the selected lights to the snapshot preset
            for a in range(len(selectedLights)):
                listConstructor.append(listBuilder(selectedLights[a]))

        customLightPresets[numOfPreset] = listConstructor

def listBuilder(selectedLight):
    paramsListBuilder = [] # the cut-down list of parameters to return to the main preset constructor

    if selectedLight == -1: # then we get the value from sendValue
        lightMACAddress = -1 # this is a global preset
        listToWorkWith = sendValue # we're using the last sent parameter on any light for this
    else: # we're recalling the params for a specific light
        # Use preferred ID or custom name if available (makes presets portable)
        prefID = availableLights[selectedLight][8] if len(availableLights[selectedLight]) > 8 else 0
        customName = availableLights[selectedLight][2]
        if prefID > 0:
            lightMACAddress = str(prefID)  # numeric ID as string
        elif customName:
            lightMACAddress = customName  # alias name
        else:
            lightMACAddress = availableLights[selectedLight][0].address  # MAC fallback
        listToWorkWith = availableLights[selectedLight][3] # we're specificially using the last parameter for the specified light for this

    if listToWorkWith != []: # if we have elements in this list, then sort them out
        if selectedLight == -1:
            # Global preset, assume light is ON (user just set this value)
            paramsListBuilder.append(listToWorkWith[1] - 130)
        elif availableLights[selectedLight][6] == False:
            paramsListBuilder.append(listToWorkWith[1] - 127) # the first value is the mode, but -127 to simplify it (and mark it as being OFF)
        else:
            paramsListBuilder.append(listToWorkWith[1] - 130) # the first value is the mode, but -130 to simplify it (and mark it as being ON)

        if listToWorkWith[1] == 135: # we're in CCT mode
            paramsListBuilder.append(listToWorkWith[3]) # the brightness
            paramsListBuilder.append(listToWorkWith[4]) # the color temperature
        elif listToWorkWith[1] == 134: # we're in HSI mode
            paramsListBuilder.append(listToWorkWith[6]) # the brightness
            paramsListBuilder.append(listToWorkWith[3] + (256 * listToWorkWith[4])) # the hue
            paramsListBuilder.append(listToWorkWith[5]) # the saturation
        elif listToWorkWith[1] == 136: # we're in ANM/SCENE
            paramsListBuilder.append(listToWorkWith[3]) # the brightness
            paramsListBuilder.append(listToWorkWith[4]) # the scene

    return [lightMACAddress, paramsListBuilder]

def customPresetToString(numOfPreset):
    returnedString = "customPreset" + str(numOfPreset) + "=" # the string to return back to the saving mechanism
    numOfLights = len(customLightPresets[numOfPreset]) # how many lights this custom preset holds values for

    for a in range(numOfLights): # get all of the lights stored in this preset (or 1 if it's a global)
        returnedString += str(customLightPresets[numOfPreset][a][0]) # get the MAC address/UUID of the nth light
        returnedString += "|" + "|".join(map(str,customLightPresets[numOfPreset][a][1])) # get a string for the rest of this current array
      
        if numOfLights > 1 and a < (numOfLights - 1): # if there are more lights left, then add a semicolon to differentiate that
            returnedString += ";"

    return returnedString

def stringToCustomPreset(presetString, numOfPreset):   
    if presetString != "|": # if the string is a valid string, then process it
        lightsToWorkWith = presetString.split(";") # split the current string into individual lights
        presetToReturn = [] # a list containing all of the preset information

        for a in range(len(lightsToWorkWith)):
            presetList = lightsToWorkWith[a].split("|") # split the current light list into its individual items
            presetPayload = [] # the actual preset list
            
            for b in range(1, len(presetList)):
                presetPayload.append(int(presetList[b]))

            if presetList[0] == "-1":
                presetToReturn.append([-1, presetPayload]) # if the light ID is -1, keep that value as an integer
            else:
                presetToReturn.append([presetList[0], presetPayload]) # if it isn't, then the MAC address is a string, so keep it that way

        return presetToReturn
    else: # if it isn't, then just return the default parameters for this preset
        return getDefaultPreset(numOfPreset)

def loadCustomPresets(presetsFilePath):
    global customLightPresets, numOfPresets, defaultLightPresets, presetNames

    # READ THE PREFERENCES FILE INTO A LIST
    with open(presetsFilePath, mode="r", encoding="utf-8") as fileToOpen:
        customPresets = fileToOpen.read().split("\n")

    # First pass: check for numOfPresets line and preset names
    for line in customPresets:
        if line.startswith("numOfPresets="):
            try:
                savedCount = int(line.split("=", 1)[1])
                if savedCount > numOfPresets:
                    numOfPresets = savedCount
                    defaultLightPresets = buildDefaultPresets(numOfPresets)
                    # Extend customLightPresets to match
                    while len(customLightPresets) < numOfPresets:
                        customLightPresets.append(getDefaultPreset(len(customLightPresets)))
            except ValueError:
                pass
        elif line.startswith("presetName"):
            # Parse lines like "presetName0=My Cool Preset"
            try:
                keyVal = line.split("=", 1)
                idx = int(keyVal[0].replace("presetName", ""))
                name = keyVal[1].strip() if len(keyVal) > 1 else ""
                if name:
                    presetNames[idx] = name
            except (ValueError, IndexError):
                pass

    # Build list of acceptable customPresetN argument names
    acceptable_arguments = ["customPreset" + str(i) for i in range(numOfPresets)]

    # Filter out non-matching lines (and the numOfPresets/presetName lines)
    filteredPresets = []
    for line in customPresets:
        if line.startswith("numOfPresets=") or line.startswith("presetName"):
            continue
        if any(x in line for x in acceptable_arguments):
            filteredPresets.append("--" + line)

    if not filteredPresets:
        return  # nothing to parse

    # Build the argument parser dynamically
    customPresetParser = argparse.ArgumentParser()
    for i in range(numOfPresets):
        customPresetParser.add_argument("--customPreset" + str(i), default=-1)

    parsedPresets = customPresetParser.parse_args(filteredPresets)

    # Apply parsed presets
    for i in range(numOfPresets):
        val = getattr(parsedPresets, "customPreset" + str(i), -1)
        if val != -1:
            customLightPresets[i] = stringToCustomPreset(val, i)
    
# RETURN THE CORRECT NAME FOR THE IDENTIFIER OF THE LIGHT (FOR DEBUG STRINGS)
def returnMACname():
    if platform.system() == "Darwin":
        return "UUID:"
    else:
        return "MAC Address:"

def _get_rssi(device, adv_data=None):
    """RSSI from AdvertisementData, falling back to BLEDevice. Bleak 0.21+ removed BLEDevice.rssi."""
    if adv_data is not None:
        try:
            return adv_data.rssi
        except (AttributeError, TypeError):
            pass
    try:
        return device.rssi
    except (AttributeError, TypeError):
        return "?"

def _get_light_rssi(light_entry):
    """RSSI display string for an availableLights entry, from index [9] or the device object."""
    if len(light_entry) > 9 and light_entry[9] is not None:
        return str(light_entry[9])
    try:
        return str(light_entry[0].rssi)
    except (AttributeError, TypeError):
        return "?"

# TEST TO MAKE SURE THE VALUE GIVEN TO THE FUNCTION IS VALID OR IN BOUNDS
def testValid(theParam, theValue, defaultValue, startBounds, endBounds, returnDefault = False):
    if theParam == "temp":
        if len(theValue) > 1: # if the temp has at least 2 characters in it
            theValue = theValue[:2] # take the first 2 characters of the string to convert into int
        else: # it either doesn't have enough characters, or isn't a number
            printDebugString(" >> error with --temp specified (not enough digits or not a number), so falling back to default value of " + str(defaultValue))
            theValue = defaultValue # default to 56(00)K for color temperature

    try: # try converting the string into an integer and processing the bounds
        theValue = int(theValue) # the value is assumed to be within the bounds, so we check it...

        if theValue < startBounds or theValue > endBounds: # the value is not within bounds, so there's an error
            if returnDefault == False: # if the value is too high or low, but we aren't set to return the defaults, make it the lowest/highest boundary
                if theValue < startBounds: # if the value specified is below the starting boundary, make it the starting boundary
                    printDebugString(" >> --" + theParam + " (" + str(theValue) + ") isn't between the bounds of " + str(startBounds) + " and " + str(endBounds) + ", so falling back to closest boundary of " + str(startBounds))
                    theValue = startBounds
                elif theValue > endBounds: # if the value specified is above the ending boundary, make it the ending boundary
                    printDebugString(" >> --" + theParam + " (" + str(theValue) + ") isn't between the bounds of " + str(startBounds) + " and " + str(endBounds) + ", so falling back to closest boundary of " + str(endBounds))
                    theValue = endBounds
            else: # if the value is too high or low, but we're set to return the default, do that here
                printDebugString(" >> --" + theParam + " (" + str(theValue) + ") isn't between the bounds of " + str(startBounds) + " and " + str(endBounds) + ", so falling back to the default value of " + str(defaultValue))
                theValue = defaultValue

        return theValue # return the within-bounds value
    except ValueError: # if the string can not be converted, then return the defaultValue
        printDebugString(" >> --" + theParam + " specified is not a number - falling back to default value of " + str(defaultValue))
        return defaultValue # return the default value

# PRINT A DEBUG STRING TO THE CONSOLE, ALONG WITH THE CURRENT TIME
_logBuffer = []  # buffered log lines for file writing
_logBufferFlushInterval = 10.0  # seconds between file flushes
_lastLogFlush = 0
_bgThreadLogCounter = 0  # throttle "Background Thread Running" messages

def printDebugString(theString):
    global _lastLogFlush, _bgThreadLogCounter
    now = datetime.now()
    currentTime = now.strftime("%H:%M:%S")
    logLine = "[" + currentTime + "] - " + theString

    if printDebug == True:
        print(logLine)

    # Emit to GUI log tab if available
    if enableLogTab:
        try:
            pass # the GUI notification that lived here is gone
        except Exception:
            pass

    # Buffer log file writes for efficiency (flush every 10s instead of per-line)
    if logToFile:
        _logBuffer.append(logLine)
        elapsed = time.time() - _lastLogFlush
        if elapsed >= _logBufferFlushInterval or len(_logBuffer) >= 50:
            try:
                with open(logFilePath, "a", encoding="utf-8") as f:
                    f.write("\n".join(_logBuffer) + "\n")
                _logBuffer.clear()
                _lastLogFlush = time.time()
            except Exception:
                pass

# CALCULATE THE BYTESTRING TO SEND TO THE LIGHT
def calculateByteString(returnValue = False, **modeArgs):
    if modeArgs["colorMode"] == "CCT":
        # We're in CCT (color balance) mode
        computedValue = [120, 135, 2, 0, 0, 0]

        computedValue[3] = int(modeArgs["brightness"]) # the brightness value
        computedValue[4] = int(modeArgs["temp"]) # the color temp value, ranging from 32(00K) to 85(00)K - some lights (like the SL-80) can go as high as 8500K
        computedValue[5] = calculateChecksum(computedValue) # compute the checksum
    elif modeArgs["colorMode"] == "HSI":
        # We're in HSI (any color of the spectrum) mode
        computedValue = [120, 134, 4, 0, 0, 0, 0, 0]

        computedValue[3] = int(modeArgs["HSI_H"]) & 255 # hue value, up to 255
        computedValue[4] = (int(modeArgs["HSI_H"]) & 65280) >> 8 # offset value, computed from above value
        computedValue[5] = int(modeArgs["HSI_S"]) # saturation value
        computedValue[6] = int(modeArgs["HSI_I"]) # intensity value
        computedValue[7] = calculateChecksum(computedValue) # compute the checksum
    elif modeArgs["colorMode"] == "ANM":
        # We're in ANM (animation) mode
        computedValue = [120, 136, 2, 0, 0, 0]

        computedValue[3] = int(modeArgs["brightness"]) # brightness value
        computedValue[4] = int(modeArgs["animation"]) # the number of animation you're going to run (check comments above)
        computedValue[5] = calculateChecksum(computedValue) # compute the checksum
    else:
        computedValue = [0]

    if returnValue == False: # if we aren't supposed to return a value, then just set sendValue to the value returned from computedValue
        global sendValue
        sendValue = computedValue
    else:
        return computedValue # return the computed value

# RECALCULATE THE BYTESTRING FOR CCT-ONLY NEEWER LIGHTS INTO HUE AND BRIGHTNESS SEPARATELY
def calculateSeparateBytestrings(sendValue):
    # CALCULATE BRIGHTNESS ONLY PARAMETER FROM MAIN PARAMETER
    newValueBRI = [120, 130, 1, sendValue[3], 0]
    newValueBRI[4] = calculateChecksum(newValueBRI)

    # CALCULATE HUE ONLY PARAMETER FROM MAIN PARAMETER
    newValueHUE = [120, 131, 1, sendValue[4], 0]
    newValueHUE[4] = calculateChecksum(newValueHUE)

    if CCTSlider == -1: # return both newly computed values
        return [newValueBRI, newValueHUE]
    elif CCTSlider == 1: # return only the color temperature value
        return newValueHUE
    elif CCTSlider == 2: # return only the brightness value
        return newValueBRI
        

def setPowerBytestring(onOrOff):
    global sendValue

    if onOrOff == "ON":
        sendValue = [120, 129, 1, 1, 251] # return the "turn on" bytestring
    else:
        sendValue = [120, 129, 1, 2, 252] # return the "turn off" bytestring

# MAKE CURRENT BYTESTRING INTO A STRING OF HEX CHARACTERS TO SHOW THE CURRENT VALUE BEING GENERATED BY THE PROGRAM
def updateStatus(splitString = False, customValue=False):
        currentHexString = ""

        if customValue == False:
            customValue = sendValue

        if splitString == False: # False is for the status bar (shows the bytestring computed as one long line)
            for a in range(len(customValue)):
                currentHexString = currentHexString + " " + str(hex(customValue[a]))
        else: # True is for the table view, this view no longer shows bytestring, but readable status of current mode (temp/bri/hue, etc.)
            currentHexString = ""

            if customValue[1] == 134:
                currentHexString = "(HSI MODE):\n"
                currentHexString = currentHexString + "  H: " + str(customValue[3] + (256 * customValue[4])) + u'\N{DEGREE SIGN}' + " / S: " + str(customValue[5]) + " / I: " + str(customValue[6])
            elif customValue[1] == 135:
                currentHexString = "(CCT MODE):\n"
                currentHexString = currentHexString + "  TEMP: " + str(customValue[4]) + "00K / BRI: " + str(customValue[3])
            elif customValue[1] == 136:
                currentHexString = "(ANM/SCENE MODE):\n"
                currentHexString = currentHexString + "  SCENE: " + str(customValue[4]) + " / BRI: " + str(customValue[3])

        return currentHexString

# CALCULATE THE CHECKSUM FROM THE BYTESTRING
def calculateChecksum(sendValue):
    checkSum = 0

    for a in range(len(sendValue) - 1):
        if sendValue[a] < 0:
            checkSum = checkSum + int(sendValue[a] + 256)
        else:
            checkSum = checkSum + int(sendValue[a])

    checkSum = checkSum & 255
    return checkSum

# FIND NEW LIGHTS
async def findDevices():
    global availableLights
    printDebugString("Searching for new lights")

    currentScan = [] # add all the current scan's lights detected to a standby array (to check against the main one)

    # Use return_adv=True to get AdvertisementData (which carries RSSI in newer Bleak)
    try:
        scan_results = await BleakScanner.discover(return_adv=True)  # returns dict[str, tuple[BLEDevice, AdvertisementData]]
    except TypeError:
        # Very old Bleak that doesn't support return_adv, fall back
        scan_results_list = await BleakScanner.discover()
        scan_results = {d.address: (d, None) for d in scan_results_list}

    for addr, (d, adv) in scan_results.items(): # go through all of the devices Bleak just found
        if d.address in whiteListedMACs: # if the MAC address is in the list of whitelisted addresses, add this device
            printDebugString("Matching whitelisted address found - " + returnMACname() + " " + d.address + ", adding to the list")
            currentScan.append((d, adv))
        else: # if this device is not whitelisted, check to see if it's valid (contains "NEEWER" in the name)
            if d.name != None and "NEEWER" in d.name: # if Bleak returned a proper string, and the string has "NEEWER" in the name
                currentScan.append((d, adv)) # add this light to this session's available lights            

    for a in range(len(currentScan)): # scan the newly found NEEWER devices
        device, adv_data = currentScan[a]
        rssi = _get_rssi(device, adv_data)
        newLight = True # initially mark this light as a "new light"

        # check the "new light" against the global list
        for b in range(len(availableLights)):
            if device.address == availableLights[b][0].address: # if the new light's MAC address matches one already in the global list
                printDebugString("Light found! [" + device.name + "] " + returnMACname() + " " + device.address + " but it's already in the list.  It may have disconnected, so relinking might be necessary.")
                newLight = False # then don't add another instance of it

                # if we found the light *again*, it's most likely the light disconnected, so we need to link it again
                availableLights[b][0] = device  # replace with fresh device object
                # Update stored RSSI (index 9), extending list if needed
                if len(availableLights[b]) > 9:
                    availableLights[b][9] = rssi
                else:
                    availableLights[b].append(rssi)
                availableLights[b][1] = "" # clear the Bleak connection (as it's changed) to force the light to need re-linking

                break # stop checking if we've found a negative result

        if newLight == True: # if this light was not found in the global list, then we need to add it
            printDebugString("Found new light! [" + device.name + "] " + returnMACname() + " " + device.address + " RSSI: " + str(rssi) + " dBm")
            customPrefs = getCustomLightPrefs(device.address, device.name)
            prefID = customPrefs[4] if len(customPrefs) > 4 else 0

            if customPrefs[3] is not None and isinstance(customPrefs[3], list): # we have previously stored parameters
                availableLights.append([device, "", customPrefs[0], customPrefs[3], customPrefs[1], customPrefs[2], True, ["---", "---"], prefID, rssi])
            else: # no stored parameters, use defaults
                availableLights.append([device, "", customPrefs[0], [120, 135, 2, 20, 56, 157], customPrefs[1], customPrefs[2], True, ["---", "---"], prefID, rssi])

    if threadAction != "quit":
        return "" # once the device scan is over, set the threadAction to nothing
    else: # if we're requesting that we quit, then just quit
        return "quit"

def getCustomLightPrefs(MACAddress, lightName = ""):
    customPrefsPath = MACAddress.split(":")
    customPrefsPath = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs" + os.sep + "".join(customPrefsPath)

    if os.path.exists(customPrefsPath):
        printDebugString("A custom preferences file was found for " + MACAddress + "!")

        # READ THE PREFERENCES FILE INTO A LIST
        with open(customPrefsPath, mode="r", encoding="utf-8") as fileToOpen:
            customPrefs = fileToOpen.read().split("|")

        if customPrefs[1] == "True": # original "wider" preference set expands color temps to 3200-8500K
            customPrefs[1] = [3200, 8500]
        elif customPrefs[1] == "False": # original "non wider" preference set color temps to 3200-5600K
            customPrefs[1] = [3200, 5600]
        elif customPrefs[1] == "": # no entry means we need to get the default value for color temps
            customPrefs[1] = getLightSpecs(lightName, "temp")
        else: # we have a new version of preferences that directly specify the color temperatures
            testPrefs = getLightSpecs(lightName, "temp")
            colorTemps = customPrefs[1].replace(" ", "").split(",")

            # TEST TO MAKE SURE VALUES RETURNED FROM colorTemps ARE VALID INTEGER VALUES
            if len(colorTemps) == 2: # we NEED to have 2 values in the list, or it's not a correct declaration (min,max)
                customPrefs[1] = [testValid("custom_preset_range_min", colorTemps[0], testPrefs[0], 1000, 5600, True),
                                  testValid("custom_preset_range_max", colorTemps[1], testPrefs[1], 1000, 10000, True)]
            else: # so if we have a different number of elements, we're wrong - revert to defaults
                printDebugString("Custom color range defined in preferences is incorrect - falling back to default values!")
                customPrefs[1] = testPrefs

        if customPrefs[2] == "True":
            customPrefs[2] = True # convert "True" as a string to an actual boolean value of True
        elif customPrefs[2] == "False":
            customPrefs[2] = False # convert "False" as a string to an actual boolean value of False
        else: # if we have no value, then get the default value for CCT enabling
            customPrefs[2] = getLightSpecs(lightName, "CCT")

        if len(customPrefs) >= 4 and customPrefs[3].strip(): # if we have a 4th element (the last used parameters), then load them here
            customPrefs[3] = customPrefs[3].replace(" ", "").split(",") # split the last params into a list

            try:
                for a in range(len(customPrefs[3])): # convert the string values to ints
                    customPrefs[3][a] = int(customPrefs[3][a])
            except ValueError:
                customPrefs[3] = None  # malformed last settings, treat as missing
        elif len(customPrefs) >= 4:
            customPrefs[3] = None  # empty lastSettings field

        # Preferred ID is the 5th field (index 4), parse it and ensure the list has it
        preferredID = 0
        if len(customPrefs) >= 5:
            try:
                preferredID = int(customPrefs[4])
            except (ValueError, IndexError):
                pass

        # Normalize: always return [name, tempRange, cctOnly, lastSettings_or_None, preferredID]
        while len(customPrefs) < 4:
            customPrefs.append(None)
        if len(customPrefs) < 5:
            customPrefs.append(preferredID)
        else:
            customPrefs[4] = preferredID

        return customPrefs
    else: # if there is no custom preferences file, still check the name against a list of per-light parameters
        specs = getLightSpecs(lightName) # get the factory default settings for this light
        # getLightSpecs returns [name, tempRange, cctOnly], extend with None lastSettings and 0 preferredID
        while len(specs) < 4:
            specs.append(None)
        if len(specs) < 5:
            specs.append(0)
        return specs

# RETURN THE DEFAULT FACTORY SPECIFICATIONS FOR LIGHTS
def getLightSpecs(lightName, returnParam = "all"):
    # the first section of lights here are LED only (can't use HSI), and the 2nd section are HSI-capable lights
    # listed with their name, the max and min color temps available to use in CCT mode, and HSI only (True) or not (False)
    masterNeewerLuxList = [
        ["Apollo", 5600, 5600, True], ["GL1", 2900, 7000, True], ["NL140", 3200, 5600, True],
        ["SNL1320", 3200, 5600, True], ["SNL1920", 3200, 5600, True], ["SNL480", 3200, 5600, True],
        ["SNL530", 3200, 5600, True], ["SNL660", 3200, 5600, True], ["SNL960", 3200, 5600, True],
        ["SRP16", 3200, 5600, True], ["SRP18", 3200, 5600, True], ["WRP18", 3200, 5600, True],
        ["ZRP16", 3200, 5600, True],
        ["BH30S", 2500, 10000, False], ["CB60", 2500, 6500, False], ["CL124", 2500, 10000, False],
        ["RGB C80", 2500, 10000, False], ["RGB CB60", 2500, 10000, False], ["RGB1000", 2500, 10000, False],
        ["RGB1200", 2500, 10000, False], ["RGB140", 2500, 10000, False], ["RGB168", 2500, 8500, False],
        ["RGB176 A1", 2500, 10000, False], ["RGB512", 2500, 10000, False], ["RGB800", 2500, 10000, False],
        ["SL-90", 2500, 10000, False], ["RGB1", 3200, 5600, False], ["RGB176", 3200, 5600, False],
        ["RGB18", 3200, 5600, False], ["RGB190", 3200, 5600, False], ["RGB450", 3200, 5600, False],
        ["RGB480", 3200, 5600, False], ["RGB530PRO", 3200, 5600, False], ["RGB530", 3200, 5600, False],
        ["RGB650", 3200, 5600, False], ["RGB660PRO", 3200, 5600, False], ["RGB660", 3200, 5600, False],
        ["RGB960", 3200, 5600, False], ["RGB-P200", 3200, 5600, False], ["RGB-P280", 3200, 5600, False],
        ["SL70", 3200, 8500, False], ["SL80", 3200, 8500, False], ["ZK-RY", 5600, 5600, False]
    ]
    
    for a in range(len(masterNeewerLuxList)): # scan the list of preset specs above to find the current light in them
        # the default list of preferences - no custom name, a color temp range from 3200-5600K, and RGB not restricted (False)
        # if we don't find the name of the light in the master list, we just return these default parameters
        customPrefs = ["", [3200, 5600], False]

        # check the master list to see if the current light is found - if it is, then change the prefs to reflect the light's spec
        if masterNeewerLuxList[a][0] in lightName.replace(" ", ""):
            # customPrefs[0] = masterNeewerLuxList[a][0] # the name of the light (for testing purposes)
            customPrefs[1] = [masterNeewerLuxList[a][1], masterNeewerLuxList[a][2]] # the HSI color temp range
            customPrefs[2] = masterNeewerLuxList[a][3] # whether or not to allow RGB commands
            break # stop looking for the light!

    if returnParam == "all": # we want to return all information (the default)
        return customPrefs
    elif returnParam == "temp": # we only want to return color temp ranges for this light
        return customPrefs[1]
    elif returnParam == "CCT": # we only want to return CCT-only status for this light
        return customPrefs[2]

# CONNECT (LINK) TO A LIGHT
async def connectToLight(selectedLight, updateGUI=True):
    global availableLights
    isConnected = False # whether or not the light is connected
    returnValue = "" # the value to return to the thread (in GUI mode, a string) or True/False (in CLI mode, a boolean value)

    lightName = availableLights[selectedLight][0].name # the Name of the light (for status updates)
    lightMAC = availableLights[selectedLight][0].address # the MAC address of the light (to keep track of the light even if the index number changes)

    createNewBleakInstance = False

    # CHECK TO SEE IF A BLEAK OBJECT EXISTS
    if availableLights[returnLightIndexesFromMacAddress(lightMAC)[0]][1] == "":
        createNewBleakInstance = True
    else: # if the object exists, but nothing is connected to it, then make a new instance
        if not availableLights[returnLightIndexesFromMacAddress(lightMAC)[0]][1].is_connected:
            createNewBleakInstance = True

    if createNewBleakInstance == True: # FILL THE [1] ELEMENT OF THE availableLights ARRAY WITH A NEW BLEAK CONNECTION OBJECT
        lightIdx = returnLightIndexesFromMacAddress(lightMAC)[0]
        device = availableLights[lightIdx][0]
        try:
            availableLights[lightIdx][1] = BleakClient(device)
        except (AttributeError, TypeError):
            # BLEDevice.details may be None if re-discovered without advertisement data
            # Fall back to MAC address string
            printDebugString("BLEDevice details unavailable for " + lightMAC + ", using address string")
            availableLights[lightIdx][1] = BleakClient(lightMAC)
        await asyncio.sleep(0.25) # wait just a short time before trying to connect

    # TRY TO CONNECT TO THE LIGHT SEVERAL TIMES BEFORE GIVING UP THE LINK
    currentAttempt = 1

    while isConnected == False and currentAttempt <= maxNumOfAttempts:
        if threadAction != "quit":
            try:
                if not availableLights[returnLightIndexesFromMacAddress(lightMAC)[0]][1].is_connected: # if the current device isn't linked to Bluetooth
                    printDebugString("Attempting to link to light [" + lightName + "] " + returnMACname() + " " + lightMAC + " (Attempt " + str(currentAttempt) + " of " + str(maxNumOfAttempts) + ")")
                    isConnected = await availableLights[returnLightIndexesFromMacAddress(lightMAC)[0]][1].connect() # try connecting it (and return the connection status)
                else:
                    isConnected = True # the light is already connected, so mark it as being connected
            except Exception as e:
                printDebugString("Error linking to light [" + lightName + "] " + returnMACname() + " " + lightMAC)
              
                if updateGUI == True:
                    if currentAttempt < maxNumOfAttempts:
                        lightIdx = returnLightIndexesFromMacAddress(lightMAC)[0]
                        if currentAttempt == 1:
                            pass # the GUI notification that lived here is gone
                            # First attempt failures are common (BLE adapter settling), show gentle status
                        else:
                            pass # the GUI notification that lived here is gone
                            # Subsequent failures are worth reporting
                else:
                    returnValue = False # if we're in CLI mode, and there is an error connecting to the light, return False

                currentAttempt = currentAttempt + 1
                if currentAttempt == 2:
                    await asyncio.sleep(1) # short retry after first failure (usually just BLE settling)
                else:
                    await asyncio.sleep(4) # longer wait for subsequent retries
        else:
            return "quit"

    if threadAction == "quit":
        return "quit"
    else:
        if isConnected == True:
            printDebugString("Successful link on light [" + lightName + "] " + returnMACname() + " " + lightMAC)

            if updateGUI == True:
                pass # the GUI notification that lived here is gone
            else:
                returnValue = True  # if we're in CLI mode, and there is no error connecting to the light, return True
        else:
            if updateGUI == True:
                pass # the GUI notification that lived here is gone

            returnValue = False # the light is not connected

    return returnValue # once the connection is over, then return either True or False (for CLI) or nothing (for GUI)

async def readNotifyCharacteristic(selectedLight, diagCommand, typeOfData):
    # clear the global variable before asking the light for info
    global receivedData
    receivedData = ""

    try:
        await availableLights[selectedLight][1].start_notify(notifyLightUUID, notifyCallback) # start reading notifications from the light
    except Exception as e:
        try: # if we've resorted the list, there is a possibility of a hanging callback, so this will raise an exception
            await availableLights[selectedLight][1].stop_notify(notifyLightUUID) # so we need to try disconnecting first
            await asyncio.sleep(0.5) # wait a little bit of time before re-connecting to the callback
            await availableLights[selectedLight][1].start_notify(notifyLightUUID, notifyCallback) # try again to start reading notifications from the light
        except Exception as e: # if we truly can't connect to the callback, return a blank string
            return "" # if there is an error starting the characteristic scan, just quit out of this routine

    for a in range(maxNumOfAttempts): # attempt maxNumOfAttempts times to read the characteristics
        try:
            await availableLights[selectedLight][1].write_gatt_char(setLightUUID, bytearray(diagCommand))
        except Exception as e:
            return "" # if there is an error checking the characteristic, just quit out of this routine

        if receivedData != "": # if the recieved data is populated
            if len(receivedData) > 1: # if we have enough elements to get a status from
                if receivedData[1] == typeOfData: # if the data returned is the correct *kind* of data
                    break # stop scanning for data
            else: # if we have a list, but it doesn't have a payload in it (the light didn't supply enough data)
                receivedData = "---" # then just re-set recievedData to the default string
                break # stop scanning for data
        else:
            await asyncio.sleep(0.25) # wait a little bit of time before checking again
    try:
        await availableLights[selectedLight][1].stop_notify(notifyLightUUID) # stop reading notifications from the light
    except Exception as e:
        pass # we will return whatever data remains from the scan, so if we can't stop the scan (light disconnected), just return what we have

    return receivedData

async def getLightChannelandPower(selectedLight):
    global availableLights
    returnInfo = ["---", "---"] # the information to return to the light

    powerInfo = await readNotifyCharacteristic(selectedLight, [120, 133, 0, 253], 2)

    try:
        if powerInfo != "":
            if powerInfo[3] == 1:
                returnInfo[0] = "ON"
            elif powerInfo[3] == 2:
                returnInfo[0] = "STBY"
        
            # IF THE LIGHT IS ON, THEN ATTEMPT TO READ THE CURRENT CHANNEL
            chanInfo = await readNotifyCharacteristic(selectedLight, [120, 132, 0, 252], 1)

            if chanInfo != "": # if we got a result from the query
                try:
                    returnInfo[1] = chanInfo[3] # set the current channel to the returned result
                except IndexError:
                    pass # if we have an index error (the above value doesn't exist), then just return -1
    except IndexError:
        # if we have an IndexError (the information returned isn't blank, but also isn't enough to descipher the status)
        # then just error out, but print the information that *was* returned for debugging purposes
        printDebugString("We don't have enough information from light [" + availableLights[selectedLight][0].name + "] to get the status.")
        print(powerInfo)

    availableLights[selectedLight][7][0] = returnInfo[0]

    if availableLights[selectedLight][1] != "---" and returnInfo[1] != "---":
        availableLights[selectedLight][7][1] = returnInfo[1]

def notifyCallback(sender, data):
    global receivedData
    receivedData = data

# DISCONNECT FROM A LIGHT
async def disconnectFromLight(selectedLight, updateGUI=True):
    returnValue = "" # same as above, string for GUI mode and boolean for CLI mode, default to blank string

    if availableLights[selectedLight][1] != "": # if there is a Bleak object attached to the light, try to disconnect
        try:
            if availableLights[selectedLight][1].is_connected: # if the current light is connected
                await availableLights[selectedLight][1].disconnect() # disconnect the selected light
        except Exception as e:
            returnValue = False # if we're in CLI mode, then return False if there is an error disconnecting

            printDebugString("Error unlinking from light " + str(selectedLight + 1) + " [" + availableLights[selectedLight][0].name + "] " + returnMACname() + " " + availableLights[selectedLight][0].address)
            print(e)

        try:
            if not availableLights[selectedLight][1].is_connected: # if the current light is NOT connected, then we're good
                if updateGUI == True: # if we're using the GUI, update the display (if we're waiting)
                    pass # the GUI notification that lived here is gone
                else: # if we're not, then indicate that we're good
                    returnValue = True # if we're in CLI mode, then return False if there is an error disconnecting

                printDebugString("Successfully unlinked from light " + str(selectedLight + 1) + " [" + availableLights[selectedLight][0].name + "] " + returnMACname() + " " + availableLights[selectedLight][0].address)
        except AttributeError:
            printDebugString("Light " + str(selectedLight + 1) + " has no Bleak object attached to it, so not attempting to disconnect from it")

    return returnValue

# WRITE TO A LIGHT - optional arguments for the CLI version (GUI version doesn't use either of these)
async def writeToLight(selectedLights=0, updateGUI=True, useGlobalValue=True):
    global availableLights
    returnValue = "" # same as above, return value "" for GUI, or boolean for CLI

    startTimer = time.time() # the start of the triggering
    printDebugString("Going into send mode")

    try:
        if updateGUI == True:
            if selectedLights == 0:
                selectedLights = [-1] # get the list of currently selected lights from the GUI table
        else:
            if type(selectedLights) is int: # if we specify an integer-based index
                selectedLights = [selectedLights] # convert asked-for light to list

        currentSendValue = [] # initialize the value check

        # if there are lights selected (otherwise just dump out), and the delay timer is less than it's maximum, then try to send to the lights selected
        while (len(selectedLights) > 0 and time.time() - startTimer < 0.4) :
            if currentSendValue != sendValue: # if the current value is different than what was last sent to the light, then send a new one
                currentSendValue = sendValue # get this value before sending to multiple lights, to ensure the same value is sent to each one

                for a in range(len(selectedLights)): # try to write each light in turn, and show the current data being sent to them in the table
                    # THIS SECTION IS FOR LOADING SNAPSHOT PRESET POWER STATES
                    if useGlobalValue == False: # if we're forcing the lights to use their stored parameters, then load that in here
                        if availableLights[selectedLights[a]][3][0] == 0: # we want to turn the light off
                            availableLights[selectedLights[a]][3][0] = 120 # reset the light's value to the normal value
                            currentSendValue = [120, 129, 1, 2, 252] # set the send value to turn the light off downstream
                        else: # we want to turn the light on and run a snapshot preset
                            await availableLights[int(selectedLights[a])][1].write_gatt_char(setLightUUID, bytearray([120, 129, 1, 1, 251]), False) # force this light to turn on
                            availableLights[int(selectedLights[a])][6] = True # set the ON flag of this light to True
                            await asyncio.sleep(0.05)

                            currentSendValue = availableLights[selectedLights[a]][3] # set the send value to set the preset downstream

                    if availableLights[selectedLights[a]][1] != "": # if a Bleak connection is there
                        try:
                            # Clamp CCT temperature to the light's effective range
                            if currentSendValue[1] == 135:  # CCT mode
                                clampedTemp = clampCCTForLight(int(selectedLights[a]), currentSendValue[4])
                                if clampedTemp is None:
                                    if updateGUI:
                                        pass # the GUI notification that lived here is gone
                                    continue  # skip this light
                                elif clampedTemp != currentSendValue[4]:
                                    currentSendValue = list(currentSendValue)
                                    currentSendValue[4] = clampedTemp
                                    currentSendValue[5] = calculateChecksum(currentSendValue)

                            if availableLights[(int(selectedLights[a]))][5] == True: # if we're using the old style of light
                                if currentSendValue[1] == 135: # if we're on CCT mode
                                    if CCTSlider == -1: # and we need to write both HUE and BRI to the light
                                        splitCommands = calculateSeparateBytestrings(currentSendValue) # get both commands from the converter

                                        # WRITE BOTH LUMINANCE AND HUE VALUES TOGETHER, BUT SEPARATELY
                                        await availableLights[int(selectedLights[a])][1].write_gatt_char(setLightUUID, bytearray(splitCommands[0]), False)
                                        await asyncio.sleep(0.05) # wait 1/20th of a second to give the Bluetooth bus a little time to recover
                                        await availableLights[int(selectedLights[a])][1].write_gatt_char(setLightUUID, bytearray(splitCommands[1]), False)
                                    else: # we're only writing either HUE or BRI independently
                                        await availableLights[int(selectedLights[a])][1].write_gatt_char(setLightUUID, bytearray(calculateSeparateBytestrings(currentSendValue)), False)
                                elif currentSendValue[1] == 129: # we're using an old light, but we're either turning the light on or off
                                    await availableLights[int(selectedLights[a])][1].write_gatt_char(setLightUUID, bytearray(currentSendValue), False)
                                elif currentSendValue[1] in (134, 136): # HSI or ANM mode on CCT-only light
                                    convertedVal = applyCCTFallback(int(selectedLights[a]), currentSendValue)
                                    if convertedVal is not None:
                                        # Send converted CCT value using split commands for old-style lights
                                        splitCommands = calculateSeparateBytestrings(convertedVal)
                                        await availableLights[int(selectedLights[a])][1].write_gatt_char(setLightUUID, bytearray(splitCommands[0]), False)
                                        await asyncio.sleep(0.05)
                                        await availableLights[int(selectedLights[a])][1].write_gatt_char(setLightUUID, bytearray(splitCommands[1]), False)
                                        currentSendValue = convertedVal  # for status display
                                    elif updateGUI == True:
                                        modeName = "HSI" if currentSendValue[1] == 134 else "ANM/SCENE"
                            else: # we're using a "newer" Neewer light, so just send the original calculated value
                                await availableLights[int(selectedLights[a])][1].write_gatt_char(setLightUUID, bytearray(currentSendValue), False)

                            if updateGUI == True:
                                # if we're not looking at an old light, or if we are, we're not in either HSI or ANM modes, then update the status of that light
                                if not (availableLights[(int(selectedLights[a]))][5] == True and (currentSendValue[1] == 134 or currentSendValue[1] == 136)):
                                    if currentSendValue[1] != 129: # if we're not turning the light on or off
                                        pass # the GUI notification that lived here is gone
                                    else: # we ARE turning the light on or off
                                        if currentSendValue[3] == 1: # we turned the light on
                                            availableLights[int(selectedLights[a])][6] = True # toggle the "light on" parameter of this light to ON

                                            changeStatus = ""

                                        else: # we turned the light off
                                            availableLights[int(selectedLights[a])][6] = False # toggle the "light on" parameter of this light to OFF

                                            changeStatus = ""
                            else:
                                returnValue = True # we successfully wrote to the light

                            if currentSendValue[1] != 129: # if we didn't just send a command to turn the light on/off
                                availableLights[selectedLights[a]][3] = currentSendValue # store the currenly sent value to recall later
                        except Exception as e:
                            if updateGUI == True:
                                pass # the GUI notification that lived here is gone
                    else: # if there is no Bleak object associated with this light (otherwise, it's been found, but not linked)
                        if updateGUI == True:
                            pass # the GUI notification that lived here is gone
                        else:
                            returnValue = 0 # the light is not linked, even though it *should* be if it gets to this point, so this is an odd error

                if useGlobalValue == True:
                    startTimer = time.time() # if we sent a value, then reset the timer
                else:
                    break # don't do the loop again (as we just want to send the commands once instead of look for newly selected lights)

            await asyncio.sleep(0.05) # wait 1/20th of a second to give the Bluetooth bus a little time to recover

            if updateGUI == True:
                selectedLights = [-1] # re-acquire the current list of selected lights
    except Exception as e:
        printDebugString("There was an error communicating with light " + str(selectedLights[a] + 1) + " [" + availableLights[selectedLights[a]][0].name + "] " + returnMACname() + " " + availableLights[selectedLights[a]][0].address)
        print(e)

        if updateGUI == True:
            returnValue = False # there was an error writing to this light, so return false to the CLI

    if updateGUI == True:
        if threadAction != "quit": # if we've been asked to quit somewhere else in the program
            printDebugString("Leaving send mode and going back to background thread")
        else:
            printDebugString("The program has requested to quit, so we're not going back to the background thread")
            returnValue = "quit"

    return returnValue

# USE THIS FUNCTION TO CONNECT TO ONE LIGHT (for CLI mode) AND RETRIEVE ANY CUSTOM PREFS (necessary for lights like the SNL-660)
async def connectToOneLight(MACAddress):
    global availableLights

    try:
        currentLightToAdd = await BleakScanner.find_device_by_address(MACAddress)
        customLightPrefs = getCustomLightPrefs(currentLightToAdd.address, currentLightToAdd.name)
        availableLights = [[currentLightToAdd, "", customLightPrefs[0], [], customLightPrefs[1], customLightPrefs[2], True, ["---", "---"], customLightPrefs[4] if len(customLightPrefs) > 4 else 0]]
    except Exception as e:
        printDebugString("Error finding the Neewer light with MAC address " + MACAddress)
        print(e)

async def parallelWriteToLights(lightIndices):
    """Write stored byte values to multiple lights simultaneously using asyncio.gather.
    Used by animation engine when parallel write mode is enabled."""
    async def writeSingleLight(idx):
        try:
            if idx < len(availableLights) and availableLights[idx][1] != "":
                currentSendValue = availableLights[idx][3]
                if currentSendValue is not None:
                    # Apply CCT-only fallback if needed
                    sendVal = applyCCTFallback(idx, currentSendValue)
                    if sendVal is None:
                        return  # ignore mode
                    # Clamp CCT temperature to effective range
                    if sendVal[1] == 135:
                        clampedTemp = clampCCTForLight(idx, sendVal[4])
                        if clampedTemp is None:
                            return  # out of range, ignore
                        elif clampedTemp != sendVal[4]:
                            sendVal = list(sendVal)
                            sendVal[4] = clampedTemp
                            sendVal[5] = calculateChecksum(sendVal)
                    if availableLights[idx][5] == True and sendVal[1] == 135:
                        # Old-style CCT light needs split commands
                        splitCmds = calculateSeparateBytestrings(sendVal)
                        await availableLights[int(idx)][1].write_gatt_char(setLightUUID, bytearray(splitCmds[0]), False)
                        await asyncio.sleep(0.05)
                        await availableLights[int(idx)][1].write_gatt_char(setLightUUID, bytearray(splitCmds[1]), False)
                    else:
                        await availableLights[int(idx)][1].write_gatt_char(setLightUUID, bytearray(sendVal), False)
        except Exception as e:
            printDebugString("Parallel write error on light " + str(idx + 1) + ": " + str(e))

    tasks = [writeSingleLight(idx) for idx in lightIndices]
    if tasks:
        await asyncio.gather(*tasks)


# THE BACKGROUND WORKER THREAD
def workerThread(_loop):
    global threadAction, _bgThreadLogCounter

    # A LIST OF LIGHTS THAT DON'T SEND POWER/CHANNEL STATUS
    lightsToNotCheckPower = ["NEEWER-RGB176"]
    hasGUI = False # the GUI is gone; these branches are dead and go with the module split

    if findLightsOnStartup == True: # if we're set to find lights at startup, then automatically set the thread to discovery mode
        threadAction = "discover"

    delayTicks = 1 # count a few ticks before checking light information
    reconnectCooldown = {} # {light_index: ticks_remaining} to avoid spamming reconnect attempts
    allDisconnectedSince = 0  # ticks since all known lights were seen as disconnected (for rescan-after-wake)

    while True:
        if delayTicks < 12:
            delayTicks += 1
        elif delayTicks == 12:
            delayTicks = 1

            # SKIP HEAVY BLE STATUS POLLING WHILE AN ANIMATION IS RUNNING
            # The status check does BLE reads on every connected light which blocks the worker
            # for 1-2 seconds, causing visible stutters in animation playback.
            if not animationRunning:
                _bgThreadLogCounter += 1
                if _bgThreadLogCounter >= 10:  # log every ~30s instead of every ~3s
                    printDebugString("Background Thread Running")
                    _bgThreadLogCounter = 0

                lightsNeedingReconnect = [] # collect lights that need reconnection this cycle
                anyConnected = False

                # CHECK EACH LIGHT AGAINST THE TABLE TO SEE IF THERE ARE CONNECTION ISSUES
                for a in range(len(availableLights)):
                    if threadAction == "": # if we're not sending, then update the light info... (check this before scanning each light)
                        if availableLights[a][1] != "": # if there is a Bleak object, then check to see if it's connected
                            if not availableLights[a][1].is_connected: # the light is disconnected, but we're reporting it isn't
                                availableLights[a][1] = "" # clear the Bleak object

                                # Queue this light for auto-reconnect if enabled
                                if autoReconnectOnDisconnect and a not in reconnectCooldown:
                                    lightsNeedingReconnect.append(a)
                                    reconnectCooldown[a] = 20  # cooldown: skip ~20 cycles (~60s) before retrying this light again
                                    printDebugString("Light " + str(a + 1) + " disconnected - will attempt auto-reconnect")
                            else:
                                anyConnected = True
                                reconnectCooldown.pop(a, None)  # light is connected, clear any cooldown
                                if not availableLights[a][0].name in lightsToNotCheckPower: # if the name of the current light is not in the list to skip checking
                                    try:
                                        _loop.run_until_complete(getLightChannelandPower(a)) # then check the power and light status of that light
                                    except Exception as e:
                                        printDebugString("Error reading power/channel for light " + str(a + 1) + ": " + str(e))
                                else: # if the light we're scanning doesn't supply power or channel status, then just show "LINKED"
                                    pass # the GUI notification that lived here is gone
                        else:
                            # Bleak object is empty - light was previously cleared; check cooldown for retry
                            if autoReconnectOnDisconnect and a not in reconnectCooldown:
                                lightsNeedingReconnect.append(a)
                                reconnectCooldown[a] = 20

                # Tick down cooldowns
                for lightIdx in list(reconnectCooldown.keys()):
                    reconnectCooldown[lightIdx] -= 1
                    if reconnectCooldown[lightIdx] <= 0:
                        del reconnectCooldown[lightIdx]

                # ATTEMPT AUTO-RECONNECT FOR DISCONNECTED LIGHTS
                if lightsNeedingReconnect and threadAction == "":
                    printDebugString("Auto-reconnect: attempting to reconnect " + str(len(lightsNeedingReconnect)) + " light(s)...")

                    # If ALL lights are disconnected, do a fresh scan first (common after sleep/wake)
                    if not anyConnected and len(availableLights) > 0:
                        allDisconnectedSince += 1
                        if allDisconnectedSince >= 2:  # after 2 consecutive all-disconnected cycles, rescan
                            printDebugString("Auto-reconnect: all lights disconnected (likely wake from sleep) - rescanning...")
                            _loop.run_until_complete(findDevices())
                            allDisconnectedSince = 0
                    else:
                        allDisconnectedSince = 0

                    # Now try connecting to each disconnected light
                    for lightIdx in lightsNeedingReconnect:
                        if threadAction == "quit":
                            break
                        if lightIdx < len(availableLights):
                            pass # the GUI notification that lived here is gone

                    try:
                        if threadAction != "quit":
                            _loop.run_until_complete(parallelAction("connect", lightsNeedingReconnect))
                            printDebugString("Auto-reconnect attempt complete")
                    except Exception as e:
                        printDebugString("Auto-reconnect error: " + str(e))

        if threadAction == "quit":
            printDebugString("Stopping the background thread")
            threadAction = "finished"
            break # stop the background thread before quitting the program
        elif threadAction == "discover":
            threadAction = _loop.run_until_complete(findDevices()) # add new lights to the main array

            if threadAction != "quit":

                if autoConnectToLights == True: # if we're set to automatically link to the lights on startup, then do it here
                    #for a in range(len(availableLights)):
                    if threadAction != "quit": # if we're not supposed to quit, then try to connect to the light(s)
                        if _isFrozenExe and len(availableLights) > 0:
                            # First connect always fails in frozen builds (WinRT init), so absorb it silently
                            printDebugString("Frozen EXE detected, performing silent BLE warm-up connect...")
                            _loop.run_until_complete(parallelAction("connect", [-1], False))
                            for _wIdx in range(len(availableLights)):
                                if availableLights[_wIdx][1] != "" and not availableLights[_wIdx][1].is_connected:
                                    availableLights[_wIdx][1] = ""
                            time.sleep(1)
                        _loop.run_until_complete(parallelAction("connect", [-1]))

                threadAction = ""
        elif threadAction == "connect":
            selectedLights = [-1] # get the list of currently selected lights

            if threadAction != "quit": # if we're not supposed to quit, then try to connect to the light(s)
                _loop.run_until_complete(parallelAction("connect", selectedLights)) # connect to each *selected* light in parallel

            threadAction = ""
        elif threadAction.startswith("httplink|"):
            # HTTP-triggered connection to specific lights (by index)
            lightIndices = [int(x) for x in threadAction.split("|")[1:]]
            if threadAction != "quit":
                _loop.run_until_complete(parallelAction("connect", lightIndices, False))
            threadAction = ""
        elif threadAction == "send":
            threadAction = _loop.run_until_complete(writeToLight()) # write a value to the light(s) - the selectedLights() section is in the write loop itself for responsiveness
        elif threadAction.startswith("psend|"):
            # Parallel animation write, send to all specified lights simultaneously
            lightIndices = [int(x) for x in threadAction.split("|")[1:]]
            printDebugString("Going into send mode")
            _loop.run_until_complete(parallelWriteToLights(lightIndices))
            printDebugString("Leaving send mode and going back to background thread")
            # Update the Status column in the light table so the user sees current values
            try:
                        if idx < len(availableLights) and availableLights[idx][3] is not None:
                            pass # the GUI notification that lived here is gone
            except Exception:
                pass
            threadAction = ""
        elif threadAction != "":
            result = processMultipleSends(_loop, threadAction)
            threadAction = result if result is not None else ""
        
        # Sleep until next cycle, but wake immediately if animation signals us
        workerWakeEvent.wait(timeout=0.25)
        workerWakeEvent.clear()

def processMultipleSends(_loop, threadAction, updateGUI = True):
    currentThreadAction = threadAction.split("|")

    if currentThreadAction[0] == "send": # this will come from loading a custom snapshot preset
        lightsToSendTo = [] # the current lights to affect

        for a in range (1, len(currentThreadAction)): # find the lights that need to be refreshed
            lightsToSendTo.append(int(currentThreadAction[a]))

        threadAction = _loop.run_until_complete(writeToLight(lightsToSendTo, updateGUI, False)) # write the value stored in the lights to the light(s)
        return threadAction

async def parallelAction(theAction, theLights, updateGUI = True):
    # SUBMIT A SERIES OF PARALLEL ASYNCIO FUNCTIONS TO RUN ALL IN PARALLEL
    parallelFuncs = []

    if theLights[0] == -1: # if we have no specific lights set, then operate on the entire availableLights range
        theLights = [] # clear the selected light list

        for a in range(len(availableLights)):
            theLights.append(a) # add all of availableLights to the list

    for a in range(len(theLights)):
        if theAction == "connect": # connect to a series of lights
            parallelFuncs.append(connectToLight(theLights[a], updateGUI))
        elif theAction == "disconnect": # disconnect from a series of lights
            parallelFuncs.append(disconnectFromLight(theLights[a], updateGUI))
        
    await asyncio.gather(*parallelFuncs) # run the functions in parallel

def processCommands(listToProcess=[]):
    inStartupMode = False # if we're in startup mode (so report that to the log), start as False initially to be set to True below

    # SET THE CURRENT LIST TO THE sys.argv SYSTEM PARAMETERS LIST IF A LIST ISN'T SPECIFIED
    # SO WE CAN USE THIS SAME FUNCTION TO PARSE HTML ARGUMENTS USING THE HTTP SERVER AND COMMAND-LINE ARGUMENTS
    if len(listToProcess) == 0: # if there aren't any elements in the list, then check against sys.argv
        listToProcess = sys.argv[1:] # the list to parse is the system args minus the first one
        inStartupMode = True

    # ADD DASHES TO ANY PARAMETERS THAT DON'T CURRENTLY HAVE THEM AS WELL AS
    # CONVERT ALL ARGUMENTS INTO lower case (to allow ALL CAPS arguments to parse correctly)
    for a in range(len(listToProcess)):
        if listToProcess[a] != "-h" and listToProcess[a][:2] != "--": # if the dashes aren't in the current item (and it's not the -h flag)
            if listToProcess[a][:1] == "-": # if the current parameter only has one dash (typed wrongly)
                listToProcess[a] = "--" + listToProcess[a][1:].lower() # then remove that, and add the double dash and switch to lowercase
            else: # the parameter has no dashes at all, so add them
                if listToProcess[a][:11] == "custom_name": # if we're setting a custom name for the light, DON'T LOWERCASE THE RESULT
                    listToProcess[a] = "--" + listToProcess[a] # add the dashes (but don't make it lowercase)
                else:
                    listToProcess[a] = "--" + listToProcess[a].lower() # add the dashes + switch to lowercase to properly parse as arguments below                  
        else: # if the dashes are already in the current item
            listToProcess[a] = listToProcess[a].lower() # we don't need to add dashes, so just switch to lowercase

    # ARGUMENTS EACH MODE HAS ACCESS TO
    acceptable_arguments = ["--light", "--mode", "--temp", "--hue", "--sat", "--bri", "--intensity",
                            "--scene", "--animation", "--list", "--on", "--off", "--force_instance"]

    # MODE-SPECIFIC ARGUMENTS
    if inStartupMode == True: # if we're using the GUI or CLI, then add these arguments to the list
        acceptable_arguments.extend(["--http", "--cli", "--silent", "--help"])
    else: # if we're using the HTTP server, then add these arguments to the list
        acceptable_arguments.extend(["--custom_name", "--discover", "--nopage", "--link", "--use_preset", "--save_preset", "--add_preset", "--delete_preset", "--batch", "--animate", "--stop_animate", "--list_animations"])

    # KICK OUT ANY PARAMETERS THAT AREN'T IN THE "ACCEPTABLE ARGUMENTS" LIST
    for a in range(len(listToProcess) - 1, -1, -1):
        if not any(x in listToProcess[a] for x in acceptable_arguments): # if the current argument is invalid
            if inStartupMode == True:
                if listToProcess[a] != "-h": # and the argument isn't "-h" (for help)
                    listToProcess.pop(a) # delete the invalid argument from the list
            else: # if we're not in startup mode, then also delete the "-h" flag
                listToProcess.pop(a) # delete the invalid argument from the list

    # IF THERE ARE NO VALID PARAMETERS LEFT TO PARSE, THEN RETURN THAT TO THE HTTP SERVER
    if inStartupMode == False and len(listToProcess) == 0:
        printDebugString("There are no usable parameters from the HTTP request!")
        return []

    # FORCE VALUES THAT NEED PARAMETERS TO HAVE ONE, AND VALUES THAT REQUIRE NO PARAMETERS TO HAVE NONE
    for a in range(len(listToProcess)):
        if listToProcess[a].find("--silent") != -1:
            listToProcess[a] = "--silent"
        elif listToProcess[a].find("--cli") != -1:
            listToProcess[a] = "--cli"
        elif listToProcess[a].find("--html") != -1:
            listToProcess[a] = "--html"
        elif listToProcess[a].find("--discover") != -1:
            listToProcess[a] = "--discover"
        elif listToProcess[a].find("--off") != -1:
            listToProcess[a] = "--off"
        elif listToProcess[a].find("--on") != -1:
            listToProcess[a] = "--on"
        elif listToProcess[a] == "--link":
            listToProcess[a] = "--link=-1"
        elif listToProcess[a] == "--custom_name":
            listToProcess[a] = "--custom_name=-1"
        elif listToProcess[a] == "--use_preset":
            listToProcess[a] = "--use_preset=-1"
        elif listToProcess[a] == "--save_preset":
            listToProcess[a] = "--save_preset=-1"
        elif listToProcess[a] == "--batch":
            listToProcess[a] = "--batch=-1"
        elif listToProcess[a] == "--animate":
            listToProcess[a] = "--animate=-1"
        elif listToProcess[a].find("--stop_animate") != -1:
            listToProcess[a] = "--stop_animate"
        elif listToProcess[a].find("--list_animations") != -1:
            listToProcess[a] = "--list_animations"

    # PARSE THE ARGUMENT LIST FOR CUSTOM PARAMETERS
    parser = argparse.ArgumentParser()

    parser.add_argument("--list", action="store_true", help="Scan for nearby Neewer lights and list them on the CLI") # list the currently available lights
    parser.add_argument("--http", action="store_true", help="Use an HTTP server to send commands to Neewer lights using a web browser")
    parser.add_argument("--silent", action="store_false", help="Don't show any debug information in the console")
    parser.add_argument("--cli", action="store_false", help="Don't show the GUI at all, just send command to one light and quit")
    parser.add_argument("--force_instance", action="store_false", help="Force a new instance of NeewerLux if another one is already running")

    # HTML SERVER SPECIFIC PARAMETERS
    if inStartupMode == False:
        parser.add_argument("--custom_name", default=-1) # a new custom name for the light
        parser.add_argument("--discover", action="store_true") # tell the HTTP server to search for newly added lights
        parser.add_argument("--link", default=-1) # link a specific light to NeewerLux
        parser.add_argument("--nopage", action="store_false") # don't render an HTML page
        parser.add_argument("--use_preset", default=-1) # number of custom preset to use via the HTTP interface
        parser.add_argument("--save_preset", default=-1) # option to save a custom snapshot preset via the HTTP interface
        parser.add_argument("--add_preset", action="store_true") # add a new preset slot
        parser.add_argument("--delete_preset", default=-1) # delete a specific preset by index
        parser.add_argument("--batch", default=-1) # send different commands to different lights simultaneously
        parser.add_argument("--animate", default=-1) # play a saved animation by name
        parser.add_argument("--stop_animate", action="store_true") # stop the currently running animation
        parser.add_argument("--list_animations", action="store_true") # list available animations

    parser.add_argument("--on", action="store_true", help="Turn the light on")
    parser.add_argument("--off", action="store_true", help="Turn the light off")
    parser.add_argument("--light", default="", help="The MAC Address (XX:XX:XX:XX:XX:XX) of the light you want to send a command to or ALL to find and control all lights (only valid when also using --cli switch)")
    parser.add_argument("--mode", default="CCT", help="[DEFAULT: CCT] The current control mode - options are HSI, CCT and either ANM or SCENE")
    parser.add_argument("--temp", "--temperature", default="56", help="[DEFAULT: 56(00)K] (CCT mode) - the color temperature (3200K+) to set the light to")
    parser.add_argument("--hue", default="240", help="[DEFAULT: 240] (HSI mode) - the hue (0-360 degrees) to set the light to")
    parser.add_argument("--sat", "--saturation", default="100", help="[DEFAULT: 100] (HSI mode) The saturation (how vibrant the color is) to set the light to")
    parser.add_argument("--bri", "--brightness", "--intensity", default="100", help="[DEFAULT: 100] (CCT/HSI/ANM mode) The brightness (intensity) to set the light to")
    parser.add_argument("--scene", "--animation", default="1", help="[DEFAULT: 1] (ANM or SCENE mode) The animation (1-9) to use in Scene mode")

    args = parser.parse_args(listToProcess)

    if args.force_instance == False: # if this value is True, then don't do anything
        global anotherInstance
        anotherInstance = False # change the global to False to allow new instances

    if args.silent == True:
        if inStartupMode == True:
            if args.list != True: # if we're not looking for lights using --list, then print line
                printDebugString("Starting program with command-line arguments")
        else:
            printDebugString("Processing HTTP arguments")
            args.cli = False # we're running the CLI, so don't initialize the GUI
            args.silent = printDebug # we're not changing the silent flag, pass on the current printDebug setting

    if args.http == True:
        return ["HTTP", args.silent] # special mode - don't do any other mode/color/etc. processing, just jump into running the HTML server

    if inStartupMode == False:
        # HTTP specific parameter returns!
        if args.custom_name != -1:
            return [None, args.nopage, args.custom_name, "custom_name"] # rename one of the lights with a new name (| delimited)

        if args.discover == True:
            return[None, args.nopage, None, "discover"] # discover new lights

        if args.link != -1:
            return[None, args.nopage, args.link, "link"] # return the value defined by the parameter

        if args.list == True:
            return [None, args.nopage, None, "list"]

        if args.use_preset != -1:
            return[None, args.nopage, testValid("use_preset", int(args.use_preset), 1, 1, numOfPresets), "use_preset"]

        if args.save_preset != -1:
            return[None, args.nopage, testValid("save_preset", int(args.save_preset), 1, 1, numOfPresets), "save_preset"]

        if args.add_preset == True:
            return[None, args.nopage, None, "add_preset"]

        if args.delete_preset != -1:
            return[None, args.nopage, int(args.delete_preset), "delete_preset"]

        if args.batch != -1:
            return[None, args.nopage, args.batch, "batch"]

        if args.animate != -1:
            return[None, args.nopage, urllib.parse.unquote(str(args.animate)), "animate"]

        if args.stop_animate == True:
            return[None, args.nopage, None, "stop_animate"]

        if args.list_animations == True:
            return[None, args.nopage, None, "list_animations"]
    else:
        # If we request "LIST" from the CLI, then return a CLI list of lights available
        if args.list == True:
            return["LIST", False]

    # CHECK TO SEE IF THE LIGHT SHOULD BE TURNED OFF
    if args.on == True: # we want to turn the light on
        return [args.cli, args.silent, args.light, "ON"]
    elif args.off == True: # we want to turn the light off
        return [args.cli, args.silent, args.light, "OFF"]

    # IF THE LIGHT ISN'T BEING TURNED OFF, CHECK TO SEE IF MODES ARE BEING SET
    if args.mode.lower() == "hsi":
        return [args.cli, args.silent, args.light, "HSI",
                testValid("hue", args.hue, 240, 0, 360),
                testValid("sat", args.sat, 100, 0, 100),
                testValid("bri", args.bri, 100, 0, 100)]
    elif args.mode.lower() in ("anm", "scene"):
        return [args.cli, args.silent, args.light, "ANM",
                testValid("scene", args.scene, 1, 1, 9),
                testValid("bri", args.bri, 100, 0, 100)]
    else: # we've either asked for CCT mode, or gave an invalid mode name
        if args.mode.lower() != "cct": # if we're not actually asking for CCT mode, display error message
            printDebugString(" >> Improper mode selected with --mode command - valid entries are")
            printDebugString(" >> CCT, HSI or either ANM or SCENE, so rolling back to CCT mode.")

        # RETURN CCT MODE PARAMETERS IN CCT/ALL OTHER CASES
        return [args.cli, args.silent, args.light, "CCT",
                testValid("temp", args.temp, 56, 32, 85),
                testValid("bri", args.bri, 100, 0, 100)]

def processHTMLCommands(paramsList, loop):
    """Process HTTP commands by queuing work for the worker thread.
    
    CRITICAL: This function runs on the HTTP server thread.  It must NEVER call
    asyncioEventLoop.run_until_complete() directly — that crashes when the worker
    thread is already using the event loop.  Instead, all BLE operations are
    queued via the global threadAction variable, which the worker thread polls.
    """
    global threadAction, numOfPresets, defaultLightPresets, customLightPresets

    # Wait briefly if worker is busy, BLE writes can take a few seconds
    for _retry in range(20):  # up to 5 seconds
        if threadAction in ("", "HTTP"):
            break
        time.sleep(0.25)
    
    if threadAction not in ("", "HTTP"):
        printDebugString("The HTTP Server requested an action, but the worker thread is busy (" + threadAction + ") after waiting. Skipping.")
        return

    if len(paramsList) == 0:
        return

    # Stop any running animation for non-animation HTTP commands
    if paramsList[3] not in ("animate", "stop_animate", "list_animations") and animationRunning:
        stopAnimation()
        try:
            pass # the GUI notification that lived here is gone
        except Exception:
            pass

    if paramsList[3] == "discover":
        # Queue discovery, worker thread handles "discover" natively (including auto-connect)
        threadAction = "discover"

    elif paramsList[3] == "link":
        selectedLights = returnLightIndexesFromMacAddress(paramsList[2])
        if len(selectedLights) > 0:
            # Queue connection via a special threadAction the worker thread handles
            threadAction = "httplink|" + "|".join(map(str, selectedLights))

    elif paramsList[3] == "use_preset":
        recallCustomPreset(paramsList[2] - 1, False, loop)

    elif paramsList[3] == "save_preset":
        presetIdx = paramsList[2] - 1
        if 0 <= presetIdx < numOfPresets:
            saveCustomPreset("snapshot", presetIdx)
            try:
                pass # the GUI notification that lived here is gone
            except Exception:
                pass
            printDebugString("HTTP: Saved snapshot preset " + str(presetIdx + 1))

    elif paramsList[3] == "add_preset":
        numOfPresets += 1
        defaultLightPresets.append(getDefaultPreset(numOfPresets - 1))
        customLightPresets.append(getDefaultPreset(numOfPresets - 1))
        try:
            pass # the GUI notification that lived here is gone
        except Exception:
            pass
        printDebugString("HTTP: Added preset #" + str(numOfPresets))

    elif paramsList[3] == "delete_preset":
        presetIdx = paramsList[2] - 1
        if 0 <= presetIdx < numOfPresets and numOfPresets > 1:
            customLightPresets.pop(presetIdx)
            defaultLightPresets.pop(presetIdx)
            newNames = {}
            for k, v in presetNames.items():
                if k < presetIdx: newNames[k] = v
                elif k > presetIdx: newNames[k - 1] = v
            presetNames.clear()
            presetNames.update(newNames)
            numOfPresets -= 1
            try:
                pass # the GUI notification that lived here is gone
            except Exception:
                pass
            printDebugString("HTTP: Deleted preset " + str(presetIdx + 1))

    elif paramsList[3] == "batch":
        processBatchCommands(paramsList[2], loop)

    elif paramsList[3] == "animate":
        animName = paramsList[2]
        if animName and animName != "-1":
            speedMult = 1.0
            fps = 5
            briScale = 1.0
            loopOverride = None
            maxLoops = 0
            revertOverride = None
            if "|" in animName:
                parts = animName.split("|")
                animName = parts[0]
                try: speedMult = float(parts[1])
                except (ValueError, IndexError): pass
                try: fps = int(parts[2])
                except (ValueError, IndexError): pass
                try: briScale = int(parts[3]) / 100.0
                except (ValueError, IndexError): pass
                # Extended parameters: loop, maxLoops, revert
                try:
                    loopVal = parts[4].strip().lower()
                    if loopVal in ("1", "true", "yes", "on"):
                        loopOverride = True
                    elif loopVal in ("0", "false", "no", "off"):
                        loopOverride = False
                except IndexError: pass
                try: maxLoops = int(parts[5])
                except (ValueError, IndexError): pass
                try:
                    revertVal = parts[6].strip().lower()
                    if revertVal in ("1", "true", "yes", "on"):
                        revertOverride = True
                    elif revertVal in ("0", "false", "no", "off"):
                        revertOverride = False
                except IndexError: pass
            # Apply revert override if specified
            if revertOverride is not None:
                global animRevertOnFinish
                animRevertOnFinish = revertOverride
            startAnimation(animName, loop, speedMult, loopOverride=loopOverride, fps=fps, briScale=briScale, maxLoops=maxLoops)
        else:
            printDebugString("HTTP: no animation name specified")

    elif paramsList[3] == "stop_animate":
        stopAnimation()

    elif paramsList[3] == "list_animations":
        pass  # handled in the HTML rendering section

    elif paramsList[3] == "custom_name":
        if paramsList[2] != "-1":
            nameInfo = paramsList[2].split("|")
            if len(nameInfo) > 1:
                nameInfo[0] = int(nameInfo[0])
                nameInfo[1] = urllib.parse.unquote(nameInfo[1])
                availableLights[nameInfo[0]][2] = nameInfo[1]
                saveLightPrefs(nameInfo[0])
                loadLightAliases()

    else:
        # CCT / HSI / ANM / ON / OFF, compute bytestring, store on lights, queue send
        if paramsList[3] == "CCT":
            computedValue = calculateByteString(True, colorMode=paramsList[3], temp=paramsList[4], brightness=paramsList[5])
        elif paramsList[3] == "HSI":
            computedValue = calculateByteString(True, colorMode=paramsList[3], HSI_H=paramsList[4], HSI_S=paramsList[5], HSI_I=paramsList[6])
        elif paramsList[3] == "ANM":
            computedValue = calculateByteString(True, colorMode=paramsList[3], animation=paramsList[4], brightness=paramsList[5])
        elif paramsList[3] == "ON":
            computedValue = [120, 129, 1, 1, 251]
        elif paramsList[3] == "OFF":
            computedValue = [120, 129, 1, 2, 252]
        else:
            printDebugString("HTTP: Unknown mode '" + paramsList[3] + "'")
            return

        selectedLights = returnLightIndexesFromMacAddress(paramsList[2])

        if len(selectedLights) > 0:
            # Store computed value in each target light's parameter slot
            for lightIdx in selectedLights:
                if lightIdx < len(availableLights):
                    availableLights[lightIdx][3] = computedValue

            # Queue the send via worker thread (parallel write)
            threadAction = "psend|" + "|".join(map(str, selectedLights))

def reorderByPreferredID():
    """Reorder availableLights so lights with preferred IDs come first (in ID order),
    followed by lights without preferred IDs (in their original discovery order).

    This ensures the GUI row numbers match the preferred IDs as closely as possible.
    For example, if lights have preferred IDs 1, 2, 3, 4, they will appear as
    rows 1, 2, 3, 4 in the table.
    """
    global availableLights

    if len(availableLights) <= 1:
        return

    withPrefID = []   # (preferredID, light)
    withoutPrefID = [] # (originalIndex, light)

    for i, light in enumerate(availableLights):
        prefID = light[8] if len(light) > 8 else 0
        if prefID > 0:
            withPrefID.append((prefID, light))
        else:
            withoutPrefID.append((i, light))

    # Sort preferred-ID lights by their ID
    withPrefID.sort(key=lambda x: x[0])

    # Rebuild: preferred-ID lights first, then discovery-order lights
    availableLights.clear()
    for _, light in withPrefID:
        availableLights.append(light)
    for _, light in withoutPrefID:
        availableLights.append(light)


def returnLightIndexesFromMacAddress(addresses):
    """Resolve light addresses/IDs/names to availableLights indices.

    Accepts:
      "*"           → all connected lights
      "1" or "2"    → numeric ID (alias-aware: if aliases define id=1 for a MAC,
                       that MAC is used regardless of discovery order)
      "Key"         → alias name (from custom name in Light Preferences)
      "D0:A8:..."   → MAC address
      "1;2;Key"     → semicolon-separated mix of the above
    """
    foundIndexes = []

    if addresses == "*" or addresses == "-1" or addresses.lower() == "all":
        for a in range(len(availableLights)):
            foundIndexes.append(a)
        return foundIndexes

    # Build reverse lookup tables from aliases
    aliasNameToMAC = {}   # {"key": "D0:A8:..."} (lowercase name → MAC)
    aliasIDToMAC = {}     # {1: "D0:A8:..."} (numeric id → MAC)
    for mac, info in lightAliases.items():
        if info.get("name"):
            aliasNameToMAC[info["name"].lower()] = mac.upper()
        if info.get("id", 0) > 0:
            aliasIDToMAC[info["id"]] = mac.upper()

    addressesToCheck = addresses.split(";")

    for addr in addressesToCheck:
        addr = addr.strip()
        if not addr:
            continue

        resolvedMAC = None

        # Try alias name match first (case-insensitive)
        if addr.lower() in aliasNameToMAC:
            resolvedMAC = aliasNameToMAC[addr.lower()]
        else:
            try:
                numericID = int(addr)
                # If aliases define a fixed ID mapping, use it
                if numericID in aliasIDToMAC:
                    resolvedMAC = aliasIDToMAC[numericID]
                else:
                    # No alias for this ID, fall back to discovery order (1-based)
                    idx = numericID - 1
                    if 0 <= idx < len(availableLights):
                        foundIndexes.append(idx)
                    continue
            except ValueError:
                # Not a number, try as MAC address
                resolvedMAC = addr.upper()

        # Resolve MAC to availableLights index
        if resolvedMAC:
            for b in range(len(availableLights)):
                if resolvedMAC == availableLights[b][0].address.upper():
                    foundIndexes.append(b)
                    break

    return foundIndexes

# ============================================================================
# BATCH COMMAND PROCESSING - send different commands to different lights at once
# ============================================================================

def parseBatchString(batchString):
    """Parse a GET-style batch string into a list of command dicts.

    Format: light:mode:p1:p2[:p3][;light:mode:p1:p2[:p3]]...
    Examples:
        1:HSI:0:100:50;2:HSI:240:100:50     (two lights, HSI mode)
        1:CCT:56:80;2:OFF;3:ON              (mixed modes including on/off)
        *:CCT:56:80                          (all lights, same command)

    Returns a list of dicts: [{"light": "1", "mode": "HSI", "hue": 0, ...}, ...]
    """
    commands = []
    segments = urllib.parse.unquote(batchString).split(";")

    for seg in segments:
        parts = seg.strip().split(":")

        # Handle MAC addresses: if we see lots of colons, the first 6 parts are a MAC
        # MAC format: XX:XX:XX:XX:XX:XX:MODE:params...
        # Simple format: lightIndex:MODE:params...
        if len(parts) < 2:
            printDebugString("Batch: skipping malformed segment '" + seg + "'")
            continue

        # Detect if this is a MAC address (6+ colon-separated hex pairs before the mode)
        isMac = False
        if len(parts) >= 8:  # at least 6 MAC octets + mode + 1 param
            try:
                for i in range(6):
                    int(parts[i], 16)
                isMac = True
            except ValueError:
                pass

        if isMac:
            lightId = ":".join(parts[0:6])  # reassemble the MAC address
            modeParts = parts[6:]
        else:
            lightId = parts[0]
            modeParts = parts[1:]

        mode = modeParts[0].upper() if modeParts else ""
        params = modeParts[1:] if len(modeParts) > 1 else []

        cmd = {"light": lightId, "mode": mode}

        if mode == "CCT":
            cmd["temp"] = int(params[0]) if len(params) > 0 else 56
            cmd["bri"] = int(params[1]) if len(params) > 1 else 100
            # Handle full temp values (e.g. 5600 -> 56)
            if cmd["temp"] > 100:
                cmd["temp"] = cmd["temp"] // 100
        elif mode == "HSI":
            cmd["hue"] = int(params[0]) if len(params) > 0 else 240
            cmd["sat"] = int(params[1]) if len(params) > 1 else 100
            cmd["bri"] = int(params[2]) if len(params) > 2 else 100
        elif mode in ("ANM", "SCENE"):
            cmd["mode"] = "ANM"
            cmd["scene"] = int(params[0]) if len(params) > 0 else 1
            cmd["bri"] = int(params[1]) if len(params) > 1 else 100
        elif mode == "ON" or mode == "OFF":
            pass  # no extra params needed
        else:
            printDebugString("Batch: unknown mode '" + mode + "' in segment '" + seg + "'")
            continue

        commands.append(cmd)

    return commands


def processBatchCommands(batchInput, loop):
    """Execute a batch of per-light commands.

    batchInput can be:
        - A string in GET batch format (parsed by parseBatchString)
        - A list of command dicts (from POST JSON)

    Each command dict: {"light": "1", "mode": "HSI", "hue": 240, "sat": 100, "bri": 50}
    """
    global availableLights

    if isinstance(batchInput, str):
        commands = parseBatchString(batchInput)
    else:
        commands = batchInput

    if not commands:
        printDebugString("Batch: no valid commands to process")
        return {"success": False, "error": "No valid commands to process", "results": []}

    printDebugString("Batch: processing " + str(len(commands)) + " command(s)")

    changedLights = []  # track which light indices we've set up
    results = []  # per-command results for JSON response

    for cmd in commands:
        lightId = str(cmd.get("light", ""))
        mode = cmd.get("mode", "").upper()

        # Resolve light identifier to indices
        selectedLights = returnLightIndexesFromMacAddress(lightId)

        if not selectedLights:
            printDebugString("Batch: could not resolve light '" + lightId + "'")
            results.append({"light": lightId, "mode": mode, "status": "error", "error": "Light not found"})
            continue

        # Compute the byte string for this command
        try:
            if mode == "ON":
                byteVal = [120, 129, 1, 1, 251]  # power on
            elif mode == "OFF":
                byteVal = [120, 129, 1, 2, 252]  # power off
            elif mode == "CCT":
                temp = max(32, min(85, int(cmd.get("temp", 56))))
                bri = max(0, min(100, int(cmd.get("bri", 100))))
                byteVal = calculateByteString(True, colorMode="CCT", brightness=bri, temp=temp)
            elif mode == "HSI":
                hue = max(0, min(360, int(cmd.get("hue", 240))))
                sat = max(0, min(100, int(cmd.get("sat", 100))))
                bri = max(0, min(100, int(cmd.get("bri", 100))))
                byteVal = calculateByteString(True, colorMode="HSI", HSI_H=hue, HSI_S=sat, HSI_I=bri)
            elif mode == "ANM":
                scene = max(1, min(9, int(cmd.get("scene", 1))))
                bri = max(0, min(100, int(cmd.get("bri", 100))))
                byteVal = calculateByteString(True, colorMode="ANM", animation=scene, brightness=bri)
            else:
                printDebugString("Batch: skipping unknown mode '" + mode + "'")
                results.append({"light": lightId, "mode": mode, "status": "error", "error": "Unknown mode"})
                continue
        except (ValueError, TypeError) as e:
            printDebugString("Batch: parameter error for light '" + lightId + "': " + str(e))
            results.append({"light": lightId, "mode": mode, "status": "error", "error": str(e)})
            continue

        # Store the computed value into each target light's parameter slot
        for lightIdx in selectedLights:
            if lightIdx < len(availableLights):
                availableLights[lightIdx][3] = byteVal
                if lightIdx not in changedLights:
                    changedLights.append(lightIdx)
                printDebugString("Batch: light " + str(lightIdx + 1) + " -> " + mode + " " + str(byteVal))

        results.append({"light": lightId, "mode": mode, "status": "ok", "targets": len(selectedLights)})

    # Now queue all changed lights for the worker thread to send
    if changedLights:
        printDebugString("Batch: queuing send to " + str(len(changedLights)) + " light(s)")
        global threadAction
        threadAction = "psend|" + "|".join(map(str, changedLights))

    return {"success": True, "commands_processed": len(commands), "lights_updated": len(changedLights), "results": results}


# ============================================================================
# CCT-ONLY FALLBACK CONVERSION
# ============================================================================

def hsiToCCTByteVal(hue, sat, bri):
    """Convert HSI parameters to a CCT byte value for CCT-only lights.
    Maps hue to warm/cool temperature: warm colors → low temp, cool → high temp."""
    cctMin = globalCCTMin // 100
    cctMax = globalCCTMax // 100
    if hue <= 60 or hue >= 300:
        temp = cctMin  # warm
    elif 60 < hue <= 150:
        temp = cctMin + int((hue - 60) / 90 * (cctMax - cctMin) * 0.5)
    elif 150 < hue <= 250:
        temp = (cctMin + cctMax) // 2 + int((hue - 150) / 100 * (cctMax - (cctMin + cctMax) // 2))
    else:
        temp = (cctMin + cctMax) // 2
    temp = max(cctMin, min(cctMax, temp))
    return calculateByteString(True, colorMode="CCT", brightness=max(0, min(100, int(bri))), temp=temp)


def getEffectiveCCTRange(lightIdx=None):
    """Get the effective CCT range for a light. Per-light overrides global.
    Returns (minK, maxK) in Kelvin (e.g. 3200, 5600)."""
    if lightIdx is not None and lightIdx < len(availableLights):
        lightRange = availableLights[lightIdx][4]
        defaultRange = getLightSpecs(availableLights[lightIdx][0].name, "temp")
        if lightRange != defaultRange:
            # Per-light custom range set, use it
            return (lightRange[0], lightRange[1])
    return (globalCCTMin, globalCCTMax)


def clampCCTForLight(lightIdx, tempValue):
    """Clamp a CCT temperature value (32-85 range) to a light's effective range.
    Returns clamped value or None if cctFallbackMode is 'ignore' and out of range."""
    minK, maxK = getEffectiveCCTRange(lightIdx)
    minVal = minK // 100
    maxVal = maxK // 100
    if minVal <= tempValue <= maxVal:
        return tempValue  # in range
    if cctFallbackMode == "convert":
        return max(minVal, min(maxVal, tempValue))  # clamp
    else:  # ignore
        return None  # out of range, skip

def applyCCTFallback(lightIdx, byteVal, mode=None, hue=0, bri=100):
    """Check if a light is CCT-only and apply fallback based on cctFallbackMode.
    Returns the (possibly converted) byte value, or None if the command should be skipped."""
    if lightIdx >= len(availableLights) or availableLights[lightIdx][5] != True:
        return byteVal  # not CCT-only, pass through

    # Check if the byte value is an HSI or ANM command
    isModeIncompat = False
    if byteVal and len(byteVal) > 1:
        if byteVal[1] == 134 or byteVal[1] == 136:  # 134=HSI, 136=ANM
            isModeIncompat = True
    if mode and mode.upper() in ("HSI", "ANM"):
        isModeIncompat = True

    if not isModeIncompat:
        return byteVal  # compatible command, pass through

    if cctFallbackMode == "ignore":
        return None  # skip this light
    elif cctFallbackMode == "convert":
        try:
            if mode and mode.upper() == "HSI":
                return hsiToCCTByteVal(hue, 100, bri)
            elif byteVal and len(byteVal) > 6 and byteVal[1] == 134:
                # Extract HSI values from the byte array
                h = byteVal[3] + (byteVal[4] << 8)
                b = byteVal[6] if len(byteVal) > 6 else 100
                return hsiToCCTByteVal(h, 100, b)
            else:
                # ANM or unknown, use neutral temp at the specified brightness
                b = byteVal[3] if byteVal and len(byteVal) > 3 else 50
                return calculateByteString(True, colorMode="CCT", brightness=max(0, min(100, b)), temp=45)
        except Exception:
            return None  # conversion failed, skip
    return byteVal  # unknown mode, pass through


# ============================================================================
# CUSTOM ANIMATION ENGINE
# ============================================================================

def interpolateHSI(start, end, t):
    """Interpolate between two HSI tuples (hue, sat, bri) at fraction t (0.0-1.0).
    Hue interpolation takes the shortest path around the 360-degree wheel."""
    h1, s1, b1 = start
    h2, s2, b2 = end

    # Shortest path hue interpolation
    diff = (h2 - h1 + 540) % 360 - 180  # wrap to [-180, 180]
    hue = (h1 + diff * t) % 360

    sat = s1 + (s2 - s1) * t
    bri = b1 + (b2 - b1) * t

    return (int(hue), int(sat), int(bri))


def interpolateCCT(start, end, t):
    """Interpolate between two CCT tuples (temp, bri) at fraction t."""
    t1, b1 = start
    t2, b2 = end
    return (int(t1 + (t2 - t1) * t), int(b1 + (b2 - b1) * t))


def animationSendFrame(frameCommands, loop):
    """Set byte values on lights and signal the worker thread to send them.
    Does NOT call any async functions or touch the event loop directly.
    Just sets values and lets the worker thread do the actual BLE writes."""
    global threadAction

    # Compute byte values for each light
    changedLights = []
    for cmd in frameCommands:
        lightId = str(cmd.get("light", ""))
        mode = cmd.get("mode", "").upper()
        selectedLights = returnLightIndexesFromMacAddress(lightId)

        if not selectedLights:
            continue

        try:
            if mode == "ON":
                byteVal = [120, 129, 1, 1, 251]
            elif mode == "OFF":
                byteVal = [120, 129, 1, 2, 252]
            elif mode == "CCT":
                temp = max(32, min(85, int(cmd.get("temp", 56))))
                bri = max(0, min(100, int(cmd.get("bri", 100))))
                byteVal = calculateByteString(True, colorMode="CCT", brightness=bri, temp=temp)
            elif mode == "HSI":
                hue = max(0, min(360, int(cmd.get("hue", 240))))
                sat = max(0, min(100, int(cmd.get("sat", 100))))
                bri = max(0, min(100, int(cmd.get("bri", 100))))
                byteVal = calculateByteString(True, colorMode="HSI", HSI_H=hue, HSI_S=sat, HSI_I=bri)
            elif mode == "ANM":
                scene = max(1, min(9, int(cmd.get("scene", 1))))
                bri = max(0, min(100, int(cmd.get("bri", 100))))
                byteVal = calculateByteString(True, colorMode="ANM", animation=scene, brightness=bri)
            else:
                continue
        except (ValueError, TypeError):
            continue

        for lightIdx in selectedLights:
            if lightIdx < len(availableLights):
                # Apply CCT-only fallback using shared helper
                actualByteVal = applyCCTFallback(lightIdx, byteVal, mode=mode,
                                                 hue=cmd.get("hue", 240), bri=cmd.get("bri", 100))
                if actualByteVal is None:
                    continue  # ignore mode, skip this light
                availableLights[lightIdx][3] = actualByteVal
                if lightIdx not in changedLights:
                    changedLights.append(lightIdx)

    if not changedLights:
        return

    # Wait for the worker thread to be free (up to 2 seconds with 10ms granularity)
    for _ in range(200):
        if threadAction == "" or threadAction == "finished":
            break
        time.sleep(0.01)
    else:
        printDebugString("Animation frame dropped: worker busy")
        return  # worker is still busy, drop this frame (logged, not silent)

    # Signal the worker thread and return immediately, do NOT wait for completion.
    # The next call's "wait for free" check handles sequencing.
    if animParallelWrites:
        threadAction = "psend|" + "|".join(map(str, changedLights))
    else:
        threadAction = "send|" + "|".join(map(str, changedLights))
    workerWakeEvent.set()


def animationEngineThread(animation, loop, speedMultiplier=1.0, loopOverride=None, fps=5, briScale=1.0, maxLoops=0):
    """Main animation playback thread. Runs keyframes with timing and interpolation.
    maxLoops: 0 = infinite (if shouldLoop), N>0 = play exactly N times then stop."""
    global animationRunning, animationStopFlag, currentAnimationName, preAnimationStates, threadAction, _animChainStop

    animationRunning = True
    animationStopFlag = False
    _animChainStop = False  # safe to clear now that the new thread is running
    shouldLoop = loopOverride if loopOverride is not None else animation.get("loop", False)
    keyframes = animation.get("keyframes", [])
    animName = animation.get("name", "Untitled")
    currentAnimationName = animName
    stepInterval = max(33, int(1000 / max(1, fps)))
    completedLoops = 0
    userStopped = False  # ms between interpolation steps (min ~30 FPS cap)

    if not keyframes:
        printDebugString("Animation '" + animName + "' has no keyframes")
        animationRunning = False
        currentAnimationName = ""
        return

    printDebugString("Animation '" + animName + "' starting (" + str(len(keyframes)) + " keyframes, loop=" + str(shouldLoop) + ", speed=" + str(speedMultiplier) + "x, rate=" + str(fps) + "/s, bri=" + str(int(briScale * 100)) + "%)")

    frameIndex = 0
    totalFrames = len(keyframes)
    prevFrameIndex = -1  # tracks previous frame for fade interpolation (-1 = none yet)
    lastGUIUpdate = 0  # throttle GUI updates to avoid cross-thread blocking

    try:
        while not animationStopFlag:
            keyframe = keyframes[frameIndex]
            hold_ms = keyframe.get("hold_ms", 200)
            fade_ms = keyframe.get("fade_ms", 0)
            lights = keyframe.get("lights", {})

            # Apply speed multiplier to timing
            hold_ms = max(50, int(hold_ms / speedMultiplier))
            fade_ms = max(0, int(fade_ms / speedMultiplier))

            # For looping animations: if we just wrapped around to frame 0 from the last frame
            # and frame 0 has fade_ms=0, auto-apply a fade using the last keyframe's fade_ms.
            # This makes loops seamless without requiring the user to set fade on the first frame.
            if frameIndex == 0 and prevFrameIndex == totalFrames - 1 and fade_ms == 0 and shouldLoop:
                lastFade = keyframes[-1].get("fade_ms", 0)
                if lastFade > 0:
                    fade_ms = max(0, int(lastFade / speedMultiplier))

            # Update GUI status (throttled to max once per second to avoid
            # cross-thread Qt blocking that causes animation stutters)
            now = time.time()
            if now - lastGUIUpdate >= 1.0:
                lastGUIUpdate = now
                try:
                    pass # the GUI notification that lived here is gone
                except Exception:
                    pass

            # === FADE TRANSITION (if fade_ms > 0 and we have a previous frame) ===
            if fade_ms > 0 and prevFrameIndex >= 0:
                prevLights = keyframes[prevFrameIndex].get("lights", {})
                steps = max(1, fade_ms // stepInterval)

                for step in range(steps):
                    if animationStopFlag:
                        break
                    t = (step + 1) / steps
                    frameCommands = []

                    for lightKey, targetParams in lights.items():
                        mode = targetParams.get("mode", "HSI").upper()
                        prevParams = prevLights.get(lightKey, targetParams)
                        prevMode = prevParams.get("mode", mode).upper()

                        # Only interpolate if modes match
                        if mode == prevMode and mode == "HSI":
                            h, s, b = interpolateHSI(
                                (prevParams.get("hue", 0), prevParams.get("sat", 100), prevParams.get("bri", 100)),
                                (targetParams.get("hue", 0), targetParams.get("sat", 100), targetParams.get("bri", 100)),
                                t
                            )
                            frameCommands.append({"light": lightKey, "mode": "HSI", "hue": h, "sat": s, "bri": max(0, min(100, int(b * briScale)))})
                        elif mode == prevMode and mode == "CCT":
                            temp, bri = interpolateCCT(
                                (prevParams.get("temp", 56), prevParams.get("bri", 100)),
                                (targetParams.get("temp", 56), targetParams.get("bri", 100)),
                                t
                            )
                            frameCommands.append({"light": lightKey, "mode": "CCT", "temp": temp, "bri": max(0, min(100, int(bri * briScale)))})
                        else:
                            # Can't interpolate across modes; just snap on last step
                            if step == steps - 1:
                                scaledTarget = dict(targetParams)
                                if "bri" in scaledTarget:
                                    scaledTarget["bri"] = max(0, min(100, int(scaledTarget["bri"] * briScale)))
                                frameCommands.append({"light": lightKey, **scaledTarget})

                    if frameCommands and not animationStopFlag:
                        animationSendFrame(frameCommands, loop)
                    time.sleep(stepInterval / 1000.0)

            else:
                # === INSTANT JUMP to this keyframe ===
                frameCommands = []
                for lightKey, params in lights.items():
                    scaledParams = dict(params)
                    if "bri" in scaledParams:
                        scaledParams["bri"] = max(0, min(100, int(scaledParams["bri"] * briScale)))
                    frameCommands.append({"light": lightKey, **scaledParams})

                if frameCommands and not animationStopFlag:
                    animationSendFrame(frameCommands, loop)

            # === HOLD on this keyframe ===
            holdSteps = max(1, hold_ms // 50)
            for _ in range(holdSteps):
                if animationStopFlag:
                    break
                time.sleep(0.05)

            # Advance to next keyframe
            prevFrameIndex = frameIndex
            frameIndex += 1
            if frameIndex >= totalFrames:
                completedLoops += 1
                if shouldLoop and not animationStopFlag:
                    # Check maxLoops: 0 = infinite, N = stop after N loops
                    if maxLoops > 0 and completedLoops >= maxLoops:
                        break  # reached loop limit
                    frameIndex = 0  # loop back to start (prevFrameIndex stays as last frame)
                else:
                    break  # animation complete

    except Exception as e:
        printDebugString("Animation engine error: " + str(e))

    userStopped = animationStopFlag
    isChaining = _animChainStop  # must be read before animationRunning=False releases stopAnimation()
    animationRunning = False
    currentAnimationName = ""
    printDebugString("Animation '" + animName + "' stopped" + (" (completed " + str(completedLoops) + " loop(s))" if completedLoops > 0 else ""))

    # Skipped when chaining: the incoming animation inherits preAnimationStates
    if animRevertOnFinish and preAnimationStates and not isChaining:
        printDebugString("Reverting lights to pre-animation state")
        changedLights = []
        for lightIdx, savedState in preAnimationStates.items():
            if lightIdx < len(availableLights):
                availableLights[lightIdx][3] = savedState
                changedLights.append(lightIdx)
        if changedLights:
            threadAction = "psend|" + "|".join(map(str, changedLights))
        preAnimationStates = {}

    try:
        pass # the GUI notification that lived here is gone
    except Exception:
        pass


def startAnimation(animName, loop, speedMultiplier=1.0, loopOverride=None, fps=5, briScale=1.0, maxLoops=0):
    """Start an animation by name. Stops any currently running animation first.
    maxLoops: 0 = use animation's loop setting, N>0 = play N times then stop."""
    global animationStopFlag, savedAnimations, preAnimationStates, _animChainStop

    # Case-insensitive name lookup, HTTP args get lowercased by the argument parser
    resolvedName = None
    for key in savedAnimations:
        if key.lower() == animName.lower():
            resolvedName = key
            break
    if resolvedName is None:
        printDebugString("Animation '" + animName + "' not found")
        return False
    animName = resolvedName

    # Chaining: keep the states captured before the first animation in the chain.
    # Cleared by the new animation thread, not here, to avoid racing the old thread's revert check.
    wasRunning = animationRunning
    if wasRunning:
        _animChainStop = True
    stopAnimation()

    if not wasRunning:
        preAnimationStates = {}
        for i, light in enumerate(availableLights):
            if light[3] is not None and isinstance(light[3], list) and len(light[3]) > 0:
                preAnimationStates[i] = list(light[3])

    animation = savedAnimations[animName].copy()
    t = threading.Thread(target=animationEngineThread,
                         args=(animation, asyncioEventLoop, speedMultiplier, loopOverride, fps, briScale, maxLoops),
                         name="animationThread", daemon=True)
    t.start()
    return True


def stopAnimation():
    """Stop the currently running animation."""
    global animationStopFlag
    if animationRunning:
        animationStopFlag = True
        # Wait up to 2 seconds for it to actually stop
        for _ in range(40):
            if not animationRunning:
                break
            time.sleep(0.05)
        printDebugString("Animation stop requested")


# ============================================================================
# ANIMATION TEMPLATES - generate animation definitions from parameters
# ============================================================================

def templatePoliceFlash(lights=None, speed_ms=300, colors=None):
    """Alternating red/blue flash between two light groups."""
    if lights is None:
        lights = ["1", "2"]
    if colors is None:
        colors = [(0, 100, 100), (240, 100, 100)]  # red, blue as (hue, sat, bri)

    halfA = lights[:len(lights)//2] if len(lights) > 1 else lights
    halfB = lights[len(lights)//2:] if len(lights) > 1 else lights

    keyframes = [
        {
            "hold_ms": speed_ms,
            "fade_ms": 0,
            "lights": {}
        },
        {
            "hold_ms": speed_ms,
            "fade_ms": 0,
            "lights": {}
        }
    ]

    for l in halfA:
        keyframes[0]["lights"][l] = {"mode": "HSI", "hue": colors[0][0], "sat": colors[0][1], "bri": colors[0][2]}
        keyframes[1]["lights"][l] = {"mode": "HSI", "hue": colors[1][0], "sat": colors[1][1], "bri": colors[1][2]}
    for l in halfB:
        keyframes[0]["lights"][l] = {"mode": "HSI", "hue": colors[1][0], "sat": colors[1][1], "bri": colors[1][2]}
        keyframes[1]["lights"][l] = {"mode": "HSI", "hue": colors[0][0], "sat": colors[0][1], "bri": colors[0][2]}

    return {
        "name": "Police Flash",
        "description": "Alternating red/blue flash",
        "loop": True,
        "keyframes": keyframes
    }


def templateColorCycle(lights=None, step_count=12, fade_ms=500, hold_ms=100, saturation=100, brightness=100):
    """Smooth hue rotation through the full spectrum on all lights."""
    if lights is None:
        lights = ["*"]

    keyframes = []
    for i in range(step_count):
        hue = int(360 * i / step_count)
        kf = {
            "hold_ms": hold_ms,
            "fade_ms": fade_ms if i > 0 else 0,
            "lights": {}
        }
        for l in lights:
            kf["lights"][l] = {"mode": "HSI", "hue": hue, "sat": saturation, "bri": brightness}
        keyframes.append(kf)

    return {
        "name": "Color Cycle",
        "description": "Smooth hue rotation through the spectrum",
        "loop": True,
        "keyframes": keyframes
    }


def templateStrobe(lights=None, on_ms=50, off_ms=50, brightness=100):
    """Rapid on/off flash."""
    if lights is None:
        lights = ["*"]

    keyframes = [
        {"hold_ms": on_ms, "fade_ms": 0, "lights": {}},
        {"hold_ms": off_ms, "fade_ms": 0, "lights": {}}
    ]
    for l in lights:
        keyframes[0]["lights"][l] = {"mode": "HSI", "hue": 0, "sat": 0, "bri": brightness}
        keyframes[1]["lights"][l] = {"mode": "HSI", "hue": 0, "sat": 0, "bri": 0}

    return {
        "name": "Strobe",
        "description": "Rapid on/off flash",
        "loop": True,
        "keyframes": keyframes
    }


def templateBreathe(lights=None, fade_ms=1500, hold_ms=200, hue=0, sat=0, max_bri=100, min_bri=5):
    """Smooth brightness fade up and down (breathing effect)."""
    if lights is None:
        lights = ["*"]

    keyframes = [
        {"hold_ms": hold_ms, "fade_ms": fade_ms, "lights": {}},
        {"hold_ms": hold_ms, "fade_ms": fade_ms, "lights": {}}
    ]
    for l in lights:
        keyframes[0]["lights"][l] = {"mode": "HSI", "hue": hue, "sat": sat, "bri": max_bri}
        keyframes[1]["lights"][l] = {"mode": "HSI", "hue": hue, "sat": sat, "bri": min_bri}

    return {
        "name": "Breathe",
        "description": "Smooth brightness fade up and down",
        "loop": True,
        "keyframes": keyframes
    }


def templateColorWash(lights=None, colors=None, fade_ms=2000, hold_ms=1000):
    """Slow transition through a list of specified colors."""
    if lights is None:
        lights = ["*"]
    if colors is None:
        colors = [(0, 100, 100), (60, 100, 100), (120, 100, 100), (180, 100, 100), (240, 100, 100), (300, 100, 100)]

    keyframes = []
    for i, (hue, sat, bri) in enumerate(colors):
        kf = {
            "hold_ms": hold_ms,
            "fade_ms": fade_ms if i > 0 else 0,
            "lights": {}
        }
        for l in lights:
            kf["lights"][l] = {"mode": "HSI", "hue": hue, "sat": sat, "bri": bri}
        keyframes.append(kf)

    return {
        "name": "Color Wash",
        "description": "Slow transition through colors",
        "loop": True,
        "keyframes": keyframes
    }


def templateRainbowChase(lights=None, step_ms=300, fade_ms=200, saturation=100, brightness=100):
    """Staggered color cycle across multiple lights (each offset in hue)."""
    if lights is None:
        lights = ["1", "2"]

    numLights = len(lights)
    step_count = max(6, numLights * 3)
    keyframes = []

    for i in range(step_count):
        kf = {
            "hold_ms": step_ms,
            "fade_ms": fade_ms if i > 0 else 0,
            "lights": {}
        }
        for idx, l in enumerate(lights):
            hueOffset = int(360 * idx / numLights)
            hue = (int(360 * i / step_count) + hueOffset) % 360
            kf["lights"][l] = {"mode": "HSI", "hue": hue, "sat": saturation, "bri": brightness}
        keyframes.append(kf)

    return {
        "name": "Rainbow Chase",
        "description": "Staggered color cycle across lights",
        "loop": True,
        "keyframes": keyframes
    }


ANIMATION_TEMPLATES = {
    "Police Flash": templatePoliceFlash,
    "Color Cycle": templateColorCycle,
    "Strobe": templateStrobe,
    "Breathe": templateBreathe,
    "Color Wash": templateColorWash,
    "Rainbow Chase": templateRainbowChase
}


# ============================================================================
# ANIMATION FILE I/O
# ============================================================================

def ensureAnimationsDir():
    """Create the animations directory if it doesn't exist."""
    try:
        os.makedirs(animationsDir, exist_ok=True)
    except OSError as e:
        printDebugString("Could not create animations directory: " + str(e))


def saveAnimationToFile(animation):
    """Save an animation dict to a JSON file."""
    ensureAnimationsDir()
    name = animation.get("name", "Untitled")
    safeName = "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()
    filePath = animationsDir + os.sep + safeName + ".json"

    try:
        with open(filePath, "w", encoding="utf-8") as f:
            json.dump(animation, f, indent=2)
        printDebugString("Saved animation '" + name + "' to " + filePath)
        return True
    except Exception as e:
        printDebugString("Error saving animation: " + str(e))
        return False


def loadAllAnimations():
    """Load all animation JSON files from the animations directory."""
    global savedAnimations
    savedAnimations = {}
    ensureAnimationsDir()

    try:
        for filename in sorted(os.listdir(animationsDir)):
            if filename.endswith(".json"):
                filePath = animationsDir + os.sep + filename
                try:
                    with open(filePath, "r", encoding="utf-8") as f:
                        anim = json.load(f)
                    name = anim.get("name", filename.replace(".json", ""))
                    savedAnimations[name] = anim
                    printDebugString("Loaded animation: " + name)
                except (json.JSONDecodeError, IOError) as e:
                    printDebugString("Error loading " + filename + ": " + str(e))
    except FileNotFoundError:
        pass

    printDebugString("Loaded " + str(len(savedAnimations)) + " animation(s)")


def loadLightAliases():
    """Build the light aliases table from per-light preferences sidecar files.

    Scans all sidecar files in light_prefs/ (named by MAC address without colons)
    and extracts custom names and preferred IDs. This is called at startup so
    aliases are available immediately for HTTP commands and animations.

    Prefs file format: customName|colorTempRange|onlyCCTMode[|lastSettings][|preferredID]
    """
    global lightAliases
    lightAliases = {}

    prefsDir = os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs"
    if not os.path.isdir(prefsDir):
        return

    for filename in os.listdir(prefsDir):
        # Sidecar files are named like D0A89E6B2084 (12 hex chars, no extension)
        if len(filename) == 12 and all(c in "0123456789ABCDEFabcdef" for c in filename):
            # Reconstruct MAC address from filename
            mac = ":".join(filename[i:i+2] for i in range(0, 12, 2)).upper()

            try:
                with open(prefsDir + os.sep + filename, mode="r", encoding="utf-8") as f:
                    fields = f.read().strip().split("|")

                customName = fields[0] if len(fields) > 0 else ""
                preferredID = 0

                # Preferred ID is in the 5th field (index 4), but if there are
                # 4 fields then [3] is lastSettings; if 5 then [4] is preferredID
                if len(fields) >= 5:
                    try:
                        preferredID = int(fields[4])
                    except (ValueError, IndexError):
                        pass
                # If there are exactly 4 fields and the 4th looks like just an int
                # (no commas), it might be a preferred ID with no lastSettings
                # But the standard format has lastSettings as comma-separated bytes
                # so this case won't arise with valid files

                if customName or preferredID > 0:
                    lightAliases[mac] = {"id": preferredID, "name": customName}
            except (IOError, OSError):
                pass

    if lightAliases:
        parts = []
        for mac, info in lightAliases.items():
            label = info["name"] if info["name"] else mac
            if info["id"] > 0:
                label += " (#" + str(info["id"]) + ")"
            parts.append(label)
        printDebugString("Loaded " + str(len(lightAliases)) + " light alias(es): " + ", ".join(parts))


def deleteAnimationFile(name):
    """Delete an animation JSON file by name."""
    safeName = "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()
    filePath = animationsDir + os.sep + safeName + ".json"
    try:
        if os.path.exists(filePath):
            os.remove(filePath)
            printDebugString("Deleted animation file: " + filePath)
            return True
    except OSError as e:
        printDebugString("Error deleting animation: " + str(e))
    return False


class NLPythonServer(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/favicon.ico": # if favicon.ico is specified, then send a 404 error and stop processing
            try:
                self.send_error(404)
            except ConnectionAbortedError:
                printDebugString("Could not serve the error page, the HTTP server is already busy with another request.")

            return
        else:
            # CHECK THE LENGTH OF THE URL REQUEST AND SEE IF IT'S TOO LONG
            if len(self.path) > 1024: # INCREASED LENGTH TO SUPPORT BATCH COMMANDS
                # THE LAST REQUEST WAS WAY TOO LONG, SO QUICKLY RENDER AN ERROR PAGE AND RETURN FROM THE HTTP RENDERER
                writeHTMLSections(self, "httpheaders")
                writeHTMLSections(self, "htmlheaders")
                writeHTMLSections(self, "quicklinks")
                writeHTMLSections(self, "errorHelp", "The last request you provided was too long!  The NeewerLux HTTP server can only accept URL commands less than 1024 characters long.")
                writeHTMLSections(self, "quicklinks")
                writeHTMLSections(self, "htmlendheaders")

                return

            # CHECK TO SEE IF THE IP REQUESTING ACCESS IS IN THE LIST OF "acceptable_HTTP_IPs"
            clientIP = self.client_address[0] # the IP address of the machine making the request
            acceptedIP = False

            for check in range(len(acceptable_HTTP_IPs)): # check all the "accepted" IP addresses against the current requesting IP
                if acceptedIP != True: # if we haven't found the IP in the accepted list, then keep checking
                    if acceptable_HTTP_IPs[check] in clientIP:
                        acceptedIP = True # if we're good to go, then we can just move on

            # IF THE IP MAKING THE REQUEST IS NOT IN THE LIST OF APPROVED ADDRESSES, THEN RETURN A "FORBIDDEN" ERROR
            if acceptedIP == False:
                self.send_error(403, "The IP of the device you're making the request from (" + clientIP + ") has to be in the list of accepted IP addresses in order to use the NeewerLux HTTP Server, any outside addresses will generate this Forbidden error.  To use this device with NeewerLux, add its IP address (or range of IP addresses) to the list of acceptable IPs")
                return

            acceptableURL = "/NeewerLux/doAction?"
            # Accept old URL path for backward compatibility
            if "/NeewerLite-Python/" in self.path:
                self.path = self.path.replace("/NeewerLite-Python/", "/NeewerLux/")

            if not acceptableURL in self.path: # serve the web dashboard
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "text/html;charset=UTF-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(getWebDashboardHTML(version=NEEWERLUX_VERSION).encode("utf-8"))

                return
            else: # if the URL contains "/NeewerLux/doAction?" then it's a valid URL
                # Check for JSON API endpoints first
                queryPart = self.path.replace(acceptableURL, "")

                # Redirect old ?list to new dashboard
                if queryPart == "list" or queryPart.startswith("list&"):
                    self.send_response(302)
                    self.send_header('Location', '/NeewerLux/')
                    self.end_headers()
                    return

                if queryPart.startswith("list_json"):
                    # Return structured JSON for the web dashboard
                    self.send_response(200)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "application/json;charset=UTF-8")
                    self.end_headers()

                    lightsData = []
                    for idx, light in enumerate(availableLights):
                        prefID = light[8] if len(light) > 8 else 0
                        effectiveID = prefID if prefID > 0 else idx + 1
                        name = light[2] if light[2] else light[0].name
                        mac = light[0].address
                        linked = bool(light[1] != "" and hasattr(light[1], 'is_connected') and light[1].is_connected)
                        try:
                            if light[3] is not None and isinstance(light[3], list) and len(light[3]) > 3:
                                status = updateStatus(True, light[3])
                            else:
                                status = ""
                        except Exception:
                            status = ""
                        lightsData.append({"id": effectiveID, "name": name, "mac": mac,
                                           "linked": linked, "status": status,
                                           "model": light[0].name})

                    animData = []
                    for aName, aData in sorted(savedAnimations.items()):
                        # Collect unique modes used in keyframes for categorization
                        modes = set()
                        for kf in aData.get("keyframes", []):
                            for lk, lp in kf.get("lights", {}).items():
                                modes.add(lp.get("mode", "HSI").upper())
                        animData.append({"name": aName,
                                         "description": aData.get("description", ""),
                                         "frames": len(aData.get("keyframes", [])),
                                         "loop": aData.get("loop", False),
                                         "modes": sorted(modes)})

                    import json as _json
                    # Build preset data for web dashboard
                    presetsData = []
                    for i in range(numOfPresets):
                        pName = presetNames.get(i, "")
                        isCustom = (i < len(customLightPresets) and i < len(defaultLightPresets)
                                    and customLightPresets[i] != defaultLightPresets[i])
                        presetsData.append({"index": i + 1, "name": pName, "custom": isCustom})

                    result = {"lights": lightsData, "animations": animData,
                              "animationPlaying": animationRunning,
                              "currentAnimation": currentAnimationName if animationRunning else "",
                              "presets": presetsData, "numPresets": numOfPresets}
                    self.wfile.write(_json.dumps(result).encode("utf-8"))
                    return

                writeHTMLSections(self, "httpheaders")

                # BREAK THE URL INTO USABLE PARAMTERS
                paramsList = self.path.replace(acceptableURL, "").split("&") # split the included params into a list
                paramsList = processCommands(paramsList) # process the commands returned from the HTTP parameters

                if len(paramsList) == 0: # we have no valid parameters, so show the error page
                    writeHTMLSections(self, "htmlheaders")
                    writeHTMLSections(self, "quicklinks")
                    writeHTMLSections(self, "errorHelp", "You didn't provide any valid parameters in the last URL.  To send multiple parameters to NeewerLux, separate each one with a & character.")
                    writeHTMLSections(self, "quicklinks")
                    writeHTMLSections(self, "htmlendheaders")
                    return
                else:
                    if paramsList[1] == True:
                        writeHTMLSections(self, "htmlheaders") # write the HTML header section
                        writeHTMLSections(self, "quicklinks-timer") # put the quicklinks (with timer) at the top of the page

                        self.wfile.write(bytes("<H1>Request Successful!</H1>\n", "utf-8"))
                        self.wfile.write(bytes("Last Request: <EM>" + self.path + "</EM><BR>\n", "utf-8"))
                        self.wfile.write(bytes("From IP: <EM>" + clientIP + "</EM><BR><BR>\n", "utf-8"))

                    if paramsList[3] != "list":
                        if paramsList[1] == True:
                            self.wfile.write(bytes("Provided Parameters:<BR>\n", "utf-8"))

                            if len(paramsList) <= 2:
                                for a in range(len(paramsList)):
                                    self.wfile.write(bytes("&nbsp;&nbsp;" + str(paramsList[a]) + "<BR>\n", "utf-8"))
                            else:
                                if paramsList[3] == "use_preset":
                                    self.wfile.write(bytes("&nbsp;&nbsp;Preset to Use: " + str(paramsList[2]) + "<BR>\n", "utf-8"))
                                elif paramsList[3] == "save_preset":
                                    pass # TODO: implement saving presets!
                                elif paramsList[3] == "batch":
                                    self.wfile.write(bytes("&nbsp;&nbsp;Batch command: " + str(paramsList[2]) + "<BR>\n", "utf-8"))
                                elif paramsList[3] == "animate":
                                    self.wfile.write(bytes("&nbsp;&nbsp;Animation: " + str(paramsList[2]) + "<BR>\n", "utf-8"))
                                    if animationRunning:
                                        self.wfile.write(bytes("&nbsp;&nbsp;Status: <STRONG>Playing</STRONG><BR>\n", "utf-8"))
                                    else:
                                        self.wfile.write(bytes("&nbsp;&nbsp;Status: Animation not found or failed to start<BR>\n", "utf-8"))
                                elif paramsList[3] == "stop_animate":
                                    self.wfile.write(bytes("&nbsp;&nbsp;Animation stopped<BR>\n", "utf-8"))
                                elif paramsList[3] == "list_animations":
                                    self.wfile.write(bytes("<H2>Saved Animations</H2>\n", "utf-8"))
                                    if len(savedAnimations) == 0:
                                        self.wfile.write(bytes("No animations saved yet.<BR>\n", "utf-8"))
                                    else:
                                        self.wfile.write(bytes("<TABLE BORDER=1 CELLPADDING=5>\n", "utf-8"))
                                        self.wfile.write(bytes("<TR><TH>Name</TH><TH>Description</TH><TH>Frames</TH><TH>Loop</TH><TH>Actions</TH></TR>\n", "utf-8"))
                                        for aName, aData in sorted(savedAnimations.items()):
                                            desc = aData.get("description", "")
                                            frames = len(aData.get("keyframes", []))
                                            loopStr = "Yes" if aData.get("loop", False) else "No"
                                            encodedName = urllib.parse.quote(aName)
                                            self.wfile.write(bytes("<TR><TD>" + aName + "</TD><TD>" + desc + "</TD><TD>" + str(frames) + "</TD><TD>" + loopStr + "</TD>", "utf-8"))
                                            self.wfile.write(bytes("<TD><A HREF='doAction?animate=" + encodedName + "'>Play</A></TD></TR>\n", "utf-8"))
                                        self.wfile.write(bytes("</TABLE>\n", "utf-8"))
                                    if animationRunning:
                                        self.wfile.write(bytes("<BR>Currently playing: <STRONG>" + currentAnimationName + "</STRONG> ", "utf-8"))
                                        self.wfile.write(bytes("<A HREF='doAction?stop_animate'>[Stop]</A><BR>\n", "utf-8"))
                                else:
                                    self.wfile.write(bytes("&nbsp;&nbsp;Parameters: " + str(paramsList[2]) + "<BR>\n", "utf-8"))

                                self.wfile.write(bytes("&nbsp;&nbsp;Mode: " + str(paramsList[3]) + "<BR>\n", "utf-8"))

                                if paramsList[3] == "CCT":
                                    self.wfile.write(bytes("&nbsp;&nbsp;Color Temperature: " + str(paramsList[4]) + "00K<BR>\n", "utf-8"))
                                    self.wfile.write(bytes("&nbsp;&nbsp;Brightness: " + str(paramsList[5]) + "<BR>\n", "utf-8"))
                                elif paramsList[3] == "HSI":
                                    self.wfile.write(bytes("&nbsp;&nbsp;Hue: " + str(paramsList[4]) + "<BR>\n", "utf-8"))
                                    self.wfile.write(bytes("&nbsp;&nbsp;Saturation: " + str(paramsList[5]) + "<BR>\n", "utf-8"))
                                    self.wfile.write(bytes("&nbsp;&nbsp;Brightness: " + str(paramsList[6]) + "<BR>\n", "utf-8"))
                                elif paramsList[3] == "ANM" or paramsList[3] == "SCENE":
                                    self.wfile.write(bytes("&nbsp;&nbsp;Animation Scene: " + str(paramsList[4]) + "<BR>\n", "utf-8"))
                                    self.wfile.write(bytes("&nbsp;&nbsp;Brightness: " + str(paramsList[5]) + "<BR>\n", "utf-8"))
                            
                            self.wfile.write(bytes("<BR><HR><BR>\n", "utf-8"))

                        # PROCESS THE HTML COMMANDS IN ANOTHER THREAD
                        htmlProcessThread = threading.Thread(target=processHTMLCommands, args=(paramsList, asyncioEventLoop), name="htmlProcessThread")
                        htmlProcessThread.start()

                    if paramsList[1] == True: # if we've been asked to list the currently available lights, do that now
                        totalLights = len(availableLights)

                        # JAVASCRIPT CODE TO CHANGE LIGHT NAMES
                        self.wfile.write(bytes("\n<!-- JAVASCRIPT CODE TO REFRESH PAGE / CHANGE LIGHT NAMES -->\n", "utf-8"))
                        self.wfile.write(bytes("<script language='JavaScript'>\n", "utf-8"))
                        self.wfile.write(bytes("  class webTimer{\n", "utf-8"))
                        self.wfile.write(bytes("    constructor(timeOut) {\n", "utf-8"))
                        self.wfile.write(bytes("      this.isRunning = true; // set to 'running' status on creation\n", "utf-8"))
                        self.wfile.write(bytes("      this.startTime = Date.now(); // the time the timer was first created\n", "utf-8"))
                        self.wfile.write(bytes("      this.timeOut = timeOut; // how long to time down from\n", "utf-8"))
                        self.wfile.write(bytes("    }\n\n", "utf-8"))
                        self.wfile.write(bytes("    stop() { // stop running the timer\n", "utf-8"))
                        self.wfile.write(bytes("      this.isRunning = false;\n", "utf-8"))
                        self.wfile.write(bytes("    }\n\n", "utf-8"))
                        self.wfile.write(bytes("    restart() { // re-start the countdown timer\n", "utf-8"))
                        self.wfile.write(bytes("      this.isRunning = true;\n", "utf-8"))
                        self.wfile.write(bytes("      this.startTime = Date.now(); // re-initialize the counter from the current time\n", "utf-8"))
                        self.wfile.write(bytes("    }\n\n", "utf-8"))
                        self.wfile.write(bytes("    getTime() {\n", "utf-8"))
                        self.wfile.write(bytes("      if (this.isRunning) { // return the amount of time that's left until the timeout\n", "utf-8"))
                        self.wfile.write(bytes("        return Math.round(this.timeOut - (Date.now() - this.startTime) / 1000);\n", "utf-8"))
                        self.wfile.write(bytes("      }\n\n", "utf-8"))
                        self.wfile.write(bytes("      return 42; // we're paused, so return a... decent answer\n", "utf-8"))
                        self.wfile.write(bytes("    }\n", "utf-8"))
                        self.wfile.write(bytes("  }\n\n", "utf-8"))
                        self.wfile.write(bytes("  function checkPageReload(ctElapsed) {\n", "utf-8"))
                        self.wfile.write(bytes("    if (ctElapsed > 0) {\n", "utf-8"))
                        self.wfile.write(bytes("      if (ctElapsed > 1) {\n", "utf-8"))
                        self.wfile.write(bytes("        document.getElementById('refreshDisplay').innerText = 'This page will auto-refresh in ' + ctElapsed + ' seconds';\n", "utf-8"))
                        self.wfile.write(bytes("      } else {\n", "utf-8"))
                        self.wfile.write(bytes("        document.getElementById('refreshDisplay').innerText = 'This page will auto-refresh in 1 second';\n", "utf-8"))
                        self.wfile.write(bytes("      }\n", "utf-8"))
                        self.wfile.write(bytes("    } else {\n", "utf-8"))
                        self.wfile.write(bytes("      location.assign('/NeewerLux/doAction?list');\n", "utf-8"))
                        self.wfile.write(bytes("    }\n", "utf-8"))
                        self.wfile.write(bytes("  }\n\n", "utf-8"))
                        self.wfile.write(bytes("  function editLight(lightNum, lightType, previousName) {\n", "utf-8"))
                        self.wfile.write(bytes("    WT.stop(); // stop the refresh timer\n\n", "utf-8"))
                        self.wfile.write(bytes("    document.getElementById('refreshDisplay').innerText = 'You clicked on an Edit button, so the refresh timer has been stopped.';\n", "utf-8"))
                        self.wfile.write(bytes("    let newName = prompt('What do you want to call light ' + (lightNum+1) + ' (' + lightType + ')?', previousName);\n\n", "utf-8"))
                        self.wfile.write(bytes("    if (!(newName == null || newName == '' || newName == previousName)) {\n", "utf-8"))
                        self.wfile.write(bytes("      window.location.href = 'doAction?custom_name=' + lightNum + '|' + newName + '';\n", "utf-8"))
                        self.wfile.write(bytes("    } else {\n", "utf-8"))
                        self.wfile.write(bytes("      WT.restart(); // restart the countdown timer for refreshing the page\n", "utf-8"))
                        self.wfile.write(bytes("    }\n", "utf-8"))
                        self.wfile.write(bytes("  }\n\n", "utf-8"))
                        self.wfile.write(bytes("  const timeOut = 8; // the delay in seconds before the page reloads\n", "utf-8"))
                        self.wfile.write(bytes("  const WT = new webTimer(timeOut); // the timer to track the above\n\n", "utf-8"))
                        self.wfile.write(bytes("  // The check to see whether or not to refresh the page\n", "utf-8"))
                        self.wfile.write(bytes("  setInterval(() => {\n", "utf-8"))
                        self.wfile.write(bytes("    const ctElapsed = WT.getTime();\n", "utf-8"))
                        self.wfile.write(bytes("    checkPageReload(ctElapsed);\n", "utf-8"))
                        self.wfile.write(bytes("  }, 250)\n", "utf-8"))
                        self.wfile.write(bytes("</script>\n\n", "utf-8"))

                        if totalLights == 0: # there are no lights available to you at the moment!
                            self.wfile.write(bytes("NeewerLux is not currently set up with any Neewer lights.  To discover new lights, <A HREF='doAction?discover'>click here</a>.<BR>\n", "utf-8"))
                        else:
                            self.wfile.write(bytes("List of available Neewer lights:<BR><BR>\n", "utf-8"))
                            self.wfile.write(bytes("<TABLE WIDTH='98%' BORDER='1'>\n", "utf-8"))
                            self.wfile.write(bytes("  <TR>\n", "utf-8"))
                            self.wfile.write(bytes("     <TH STYLE='width:2%; text-align:left'>ID #\n", "utf-8"))
                            self.wfile.write(bytes("     <TH STYLE='width:18%; text-align:left'>Custom Name</TH>\n", "utf-8"))
                            self.wfile.write(bytes("     <TH STYLE='width:18%; text-align:left'>Light Type</TH>\n", "utf-8"))
                            self.wfile.write(bytes("     <TH STYLE='width:30%; text-align:left'>MAC Address/GUID</TH>\n", "utf-8"))
                            self.wfile.write(bytes("     <TH STYLE='width:5%; text-align:left'>RSSI</TH>\n", "utf-8"))
                            self.wfile.write(bytes("     <TH STYLE='width:5%; text-align:left'>Linked</TH>\n", "utf-8"))
                            self.wfile.write(bytes("     <TH STYLE='width:22%; text-align:left'>Last Sent Value</TH>\n", "utf-8"))
                            self.wfile.write(bytes("  </TR>\n", "utf-8"))

                            for a in range(totalLights):
                                self.wfile.write(bytes("  <TR>\n", "utf-8"))
                                self.wfile.write(bytes("     <TD STYLE='background-color:rgb(173,255,47)'>" + str(a + 1) + "</TD>\n", "utf-8")) # light ID #
                                self.wfile.write(bytes("     <TD STYLE='background-color:rgb(240,248,255)'><button onclick='editLight(" + str(a) + ", \"" + availableLights[a][0].name + "\", \"" + availableLights[a][2] + "\")'>Edit</button>&nbsp;&nbsp;" + availableLights[a][2] + "</TD>\n", "utf-8")) # light custom name
                                self.wfile.write(bytes("     <TD STYLE='background-color:rgb(240,248,255)'>" + availableLights[a][0].name + "</TD>\n", "utf-8")) # light type
                                self.wfile.write(bytes("     <TD STYLE='background-color:rgb(240,248,255)'>" + availableLights[a][0].address + "</TD>\n", "utf-8")) # light MAC address
                                self.wfile.write(bytes("     <TD STYLE='background-color:rgb(240,248,255)'>" + _get_light_rssi(availableLights[a]) + " dBm</TD>\n", "utf-8")) # light RSSI (signal quality)

                                try:
                                    if availableLights[a][1].is_connected:
                                        self.wfile.write(bytes("     <TD STYLE='background-color:rgb(240,248,255)'>" + "Yes" + "</TD>\n", "utf-8")) # is the light linked?
                                    else:
                                        self.wfile.write(bytes("     <TD STYLE='background-color:rgb(240,248,255)'>" + "<A HREF='doAction?link=" + str(a + 1) + "'>No</A></TD>\n", "utf-8")) # is the light linked?
                                except Exception as e:
                                    self.wfile.write(bytes("     <TD STYLE='background-color:rgb(240,248,255)'>" + "<A HREF='doAction?link=" + str(a + 1) + "'>No</A></TD>\n", "utf-8")) # is the light linked?

                                self.wfile.write(bytes("     <TD STYLE='background-color:rgb(240,248,255)'>" + updateStatus(False, availableLights[a][3]) + "</TD>\n", "utf-8")) # the last sent value to the light
                                self.wfile.write(bytes("  </TR>\n", "utf-8"))

                            self.wfile.write(bytes("</TABLE>\n", "utf-8"))

                        self.wfile.write(bytes("<BR><HR><BR>\n", "utf-8"))
                        self.wfile.write(bytes("<A ID='presets'>List of available custom presets to use:</A><BR><BR>\n", "utf-8"))
                        self.wfile.write(bytes("<TABLE WIDTH='98%' BORDER='1'>\n", "utf-8"))
                        self.wfile.write(bytes("  <TR>\n", "utf-8"))
                        self.wfile.write(bytes("     <TH STYLE='width:4%; text-align:left'>Preset\n", "utf-8"))
                        self.wfile.write(bytes("     <TH STYLE='width:46%; text-align:left'>Preset Parameters</TH>\n", "utf-8"))
                        self.wfile.write(bytes("     <TH STYLE='width:4%; text-align:left'>Preset\n", "utf-8"))
                        self.wfile.write(bytes("     <TH STYLE='width:46%; text-align:left'>Preset Parameters</TH>\n", "utf-8"))
                        self.wfile.write(bytes("  </TR>\n", "utf-8"))
                        
                        for a in range((numOfPresets + 1) // 2): # build the list itself, showing 2 presets next to each other
                            currentPreset = (2 * a)
                            self.wfile.write(bytes("  <TR>\n", "utf-8"))
                            self.wfile.write(bytes("     <TD ALIGN='CENTER' STYLE='background-color:rgb(173,255,47)'><FONT SIZE='+2'><A HREF='doAction?use_preset=" + str(currentPreset + 1) + "'>" + str(currentPreset + 1) + "</A></FONT></TD>\n", "utf-8"))
                            self.wfile.write(bytes("     <TD VALIGN='TOP' STYLE='background-color:rgb(240,248,255)'>" + customPresetInfoBuilder(currentPreset, True) + "</TD>\n", "utf-8"))
                            if currentPreset + 1 < numOfPresets:
                                self.wfile.write(bytes("     <TD ALIGN='CENTER' STYLE='background-color:rgb(173,255,47)'><FONT SIZE='+2'><A HREF='doAction?use_preset=" + str(currentPreset + 2) + "'>" + str(currentPreset + 2) + "</A></FONT></TD>\n", "utf-8"))
                                self.wfile.write(bytes("     <TD VALIGN='TOP' STYLE='background-color:rgb(240,248,255)'>" + customPresetInfoBuilder(currentPreset + 1, True) + "</TD>\n", "utf-8"))
                            else:
                                self.wfile.write(bytes("     <TD></TD><TD></TD>\n", "utf-8"))
                            self.wfile.write(bytes("  </TR>\n", "utf-8"))
                        
                        self.wfile.write(bytes("</TABLE>\n", "utf-8"))
            
            if paramsList[1] == True:
                writeHTMLSections(self, "quicklinks") # add the footer to the bottom of the page
                writeHTMLSections(self, "htmlendheaders") # add the ending section to the very bottom

    def do_POST(self):
        """Handle POST requests for batch commands with JSON body.

        POST /NeewerLux/batch
        Content-Type: application/json

        Request body:
        {
            "commands": [
                {"light": "1", "mode": "HSI", "hue": 0, "sat": 100, "bri": 50},
                {"light": "2", "mode": "CCT", "temp": 56, "bri": 80},
                {"light": "3", "mode": "ON"},
                {"light": "*", "mode": "OFF"}
            ]
        }

        Response: JSON with per-command results.
        """
        # IP CHECK (same as do_GET)
        clientIP = self.client_address[0]
        acceptedIP = False
        for check in range(len(acceptable_HTTP_IPs)):
            if acceptedIP != True:
                if acceptable_HTTP_IPs[check] in clientIP:
                    acceptedIP = True

        if acceptedIP == False:
            self.send_error(403, "Forbidden - IP " + clientIP + " is not in the list of accepted addresses")
            return

        # ONLY ACCEPT REQUESTS TO SUPPORTED POST ENDPOINTS
        if self.path not in ("/NeewerLux/batch", "/NeewerLux/animate", "/NeewerLite-Python/batch", "/NeewerLite-Python/animate"):
            self.send_error(404, "POST requests are supported at /NeewerLux/batch and /NeewerLux/animate")
            return

        # READ AND PARSE THE JSON BODY
        try:
            contentLength = int(self.headers.get("Content-Length", 0))
            if contentLength > 65536:  # 64KB sanity limit
                self.send_error(413, "Request body too large (max 64KB)")
                return
            if contentLength == 0:
                self.send_error(400, "Empty request body")
                return

            rawBody = self.rfile.read(contentLength)
            body = json.loads(rawBody.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.send_error(400, "Invalid JSON: " + str(e))
            return

        resultData = {}

        if self.path in ("/NeewerLux/batch", "/NeewerLite-Python/batch"):
            commands = body.get("commands", [])
            if not isinstance(commands, list) or len(commands) == 0:
                self.send_error(400, "Request must contain a non-empty 'commands' array")
                return

            printDebugString("POST batch request from " + clientIP + " with " + str(len(commands)) + " command(s)")

            global threadAction
            if threadAction != "" and threadAction != "finished":
                resultData = {"success": False, "error": "Server is busy processing another command. Please retry."}
            else:
                threadAction = "HTTP"
                try:
                    resultData = processBatchCommands(commands, asyncioEventLoop)
                except Exception as e:
                    resultData = {"success": False, "error": str(e)}
                finally:
                    threadAction = ""

        elif self.path in ("/NeewerLux/animate", "/NeewerLite-Python/animate"):
            action = body.get("action", "play")  # play, stop, list, create
            printDebugString("POST animate request from " + clientIP + ": " + action)

            if action == "play":
                animName = body.get("name", "")
                speed = body.get("speed", 1.0)
                loop = body.get("loop", None)
                fps = body.get("rate", body.get("fps", 5))
                briScale = body.get("brightness", body.get("bri", 100)) / 100.0
                if "parallel" in body:
                    global animParallelWrites
                    animParallelWrites = bool(body.get("parallel", True))
                if animName:
                    # Case-insensitive name resolution
                    resolvedAnimName = None
                    for key in savedAnimations:
                        if key.lower() == animName.lower():
                            resolvedAnimName = key
                            break
                if resolvedAnimName:
                    animName = resolvedAnimName
                    maxLoops = int(body.get("maxLoops", body.get("max_loops", 0)))
                    # Handle revert flag from web UI
                    if "revert" in body:
                        global animRevertOnFinish
                        animRevertOnFinish = bool(body.get("revert", True))
                    startAnimation(animName, asyncioEventLoop, speed, loop, fps=int(fps), briScale=briScale, maxLoops=maxLoops)
                    resultData = {"success": True, "action": "play", "animation": animName}
                else:
                    resultData = {"success": False, "error": "Animation '" + str(animName) + "' not found",
                                  "available": list(savedAnimations.keys())}
            elif action == "stop":
                stopAnimation()
                resultData = {"success": True, "action": "stop"}
            elif action == "list":
                animList = []
                for aName, aData in sorted(savedAnimations.items()):
                    animList.append({
                        "name": aName,
                        "description": aData.get("description", ""),
                        "keyframes": len(aData.get("keyframes", [])),
                        "loop": aData.get("loop", False)
                    })
                resultData = {"success": True, "animations": animList,
                              "currently_playing": currentAnimationName if animationRunning else None}
            elif action == "create":
                # Create a new animation from JSON definition
                animData = body.get("animation", {})
                if not animData.get("name") or not animData.get("keyframes"):
                    resultData = {"success": False, "error": "Animation must have 'name' and 'keyframes'"}
                else:
                    savedAnimations[animData["name"]] = animData
                    saveAnimationToFile(animData)
                    resultData = {"success": True, "action": "create", "animation": animData["name"]}
                    # Refresh GUI list if available
                    try:
                        pass # the GUI notification that lived here is gone
                    except Exception:
                        pass
            elif action == "status":
                resultData = {"success": True, "running": animationRunning,
                              "animation": currentAnimationName if animationRunning else None}
            else:
                resultData = {"success": False, "error": "Unknown action: " + action}

        # SEND JSON RESPONSE
        responseBody = json.dumps(resultData).encode("utf-8")
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(responseBody)))
        self.end_headers()
        self.wfile.write(responseBody)

def writeHTMLSections(self, theSection, errorMsg = ""):
    if theSection == "httpheaders":
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/html;charset=UTF-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
    elif theSection == "htmlheaders":
        self.wfile.write(bytes("<!DOCTYPE html>\n", "utf-8"))
        self.wfile.write(bytes("<HTML>\n<HEAD>\n", "utf-8"))
        self.wfile.write(bytes("<TITLE>NeewerLux " + NEEWERLUX_VERSION + " — based on NeewerLite-Python by Zach Glenwright / NeewerLite by Xu Lian</TITLE>\n</HEAD>\n", "utf-8"))
        self.wfile.write(bytes("<BODY>\n", "utf-8"))
    elif theSection == "errorHelp":
        self.wfile.write(bytes("<H1>Invalid request!</H1>\n", "utf-8"))
        self.wfile.write(bytes("Last Request: <EM>" + self.path + "</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes(errorMsg + "<BR><BR>\n", "utf-8"))
        self.wfile.write(bytes("Valid parameters to use -<BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>list</STRONG> - list the current lights NeewerLux has available to it and the custom presets it can use<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?list</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>discover</STRONG> - tell NeewerLux to scan for new lights<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?discover</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>nopage</STRONG> - send a command to the HTTP server, but don't render the webpage showing the results (<EM>useful, for example, on a headless Raspberry Pi where you don't necessarily want to see the results page</EM>)<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?nopage</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>link=</STRONG> - (value: <EM>index of light to link to</EM>) manually link to a specific light - you can specify multiple lights with semicolons (so link=1;2 would try to link to both lights 1 and 2)<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?link=1</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>light=</STRONG> - the MAC address (or current index of the light) you want to send a command to - you can specify multiple lights with semicolons (so light=1;2 would send a command to both lights 1 and 2)<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?light=11:22:33:44:55:66</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>mode=</STRONG> - the mode (value: <EM>HSI, CCT, and either ANM or SCENE</EM>) - the color mode to switch the light to<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?mode=CCT</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>use_preset=</STRONG> - (value: <EM>1-" + str(numOfPresets) + "</EM>) - use a custom global or snapshot preset<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?use_preset=2</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("(CCT mode only) <STRONG>temp=</STRONG> or <STRONG>temperature=</STRONG> - (value: <EM>3200 to 8500</EM>) the color temperature in CCT mode to set the light to<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?temp=5200</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("(HSI mode only) <STRONG>hue=</STRONG> - (value: <EM>0 to 360</EM>) the hue value in HSI mode to set the light to<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?hue=240</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("(HSI mode only) <STRONG>sat=</STRONG> or <STRONG>saturation=</STRONG> - (value: <EM>0 to 100</EM>) the color saturation value in HSI mode to set the light to<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?sat=65</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("(ANM/SCENE mode only) <STRONG>scene=</STRONG> - (value: <EM>1 to 9</EM>) which animation (scene) to switch the light to<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?scene=3</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("(CCT/HSI/ANM modes) <STRONG>bri=</STRONG>, <STRONG>brightness=</STRONG> or <STRONG>intensity=</STRONG> - (value: <EM>0 to 100</EM>) how bright you want the light<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?brightness=80</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<BR><BR>More examples -<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Set the light with MAC address <EM>11:22:33:44:55:66</EM> to <EM>CCT</EM> mode, with a color temperature of <EM>5200</EM> and brightness of <EM>40</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;<EM>http://(server address)/NeewerLux/doAction?light=11:22:33:44:55:66&mode=CCT&temp=5200&bri=40</EM><BR><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Set the light with MAC address <EM>11:22:33:44:55:66</EM> to <EM>HSI</EM> mode, with a hue of <EM>70</EM>, saturation of <EM>50</EM> and brightness of <EM>10</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;<EM>http://(server address)/NeewerLux/doAction?light=11:22:33:44:55:66&mode=HSI&hue=70&sat=50&bri=10</EM><BR><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Set the first light available to <EM>SCENE</EM> mode, using the <EM>first</EM> animation and brightness of <EM>55</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;<EM>http://(server address)/NeewerLux/doAction?light=1&mode=SCENE&scene=1&bri=55</EM><BR><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Use the 2nd custom preset, but don't render the webpage showing the results<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;<EM>http://(server address)/NeewerLux/doAction?use_preset=2&nopage</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<BR><HR><BR>\n", "utf-8"))
        self.wfile.write(bytes("<H2>Batch Commands (multiple lights, different settings)</H2>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>batch=</STRONG> - send different commands to different lights in a single request<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Format: <EM>light:mode:param1:param2[:param3]</EM> — separate multiple commands with semicolons<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Modes: <STRONG>CCT</STRONG> (temp:bri), <STRONG>HSI</STRONG> (hue:sat:bri), <STRONG>ANM</STRONG> (scene:bri), <STRONG>ON</STRONG>, <STRONG>OFF</STRONG><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Lights can be specified by index (1, 2...), MAC address, or * for all<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;CCT temp values: use either short (56) or full (5600) format — both become 5600K<BR><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Examples:<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Set light 1 to red and light 2 to blue:<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;<EM>http://(server address)/NeewerLux/doAction?batch=1:HSI:0:100:100;2:HSI:240:100:100</EM><BR><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Set light 1 to warm CCT, turn light 2 off:<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;<EM>http://(server address)/NeewerLux/doAction?batch=1:CCT:32:80;2:OFF</EM><BR><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Set all lights to 5600K at 50% brightness:<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;<EM>http://(server address)/NeewerLux/doAction?batch=*:CCT:56:50</EM><BR><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>POST /NeewerLux/batch</STRONG> - same as above, but using a JSON body for richer control<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Send a POST request with Content-Type: application/json<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Body: <EM>{\"commands\": [{\"light\": \"1\", \"mode\": \"HSI\", \"hue\": 0, \"sat\": 100, \"bri\": 50}, ...]}</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Returns a JSON response with per-command results<BR>\n", "utf-8"))
        self.wfile.write(bytes("<BR><HR><BR>\n", "utf-8"))
        self.wfile.write(bytes("<H2>Custom Animations</H2>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>list_animations</STRONG> - list all saved custom animations<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?list_animations</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>animate=</STRONG> - play a saved animation by name (URL-encode spaces as %20)<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Optionally append |speed|rate|brightness (e.g. animate=Police%20Flash|2.0|10|50)<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?animate=Police%20Flash</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?animate=Color%20Cycle|0.5</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>stop_animate</STRONG> - stop the currently playing animation<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;&nbsp;&nbsp;Example: <EM>http://(server address)/NeewerLux/doAction?stop_animate</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("<STRONG>POST /NeewerLux/animate</STRONG> - JSON API for animation control<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Actions: play, stop, list, create, status<BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Play: <EM>{\"action\": \"play\", \"name\": \"Police Flash\", \"speed\": 1.0, \"loop\": true, \"rate\": 5}</EM><BR>\n", "utf-8"))
        self.wfile.write(bytes("&nbsp;&nbsp;Create: <EM>{\"action\": \"create\", \"animation\": {\"name\": \"...\", \"loop\": true, \"keyframes\": [...]}}</EM><BR>\n", "utf-8"))
    elif theSection == "quicklinks" or theSection == "quicklinks-timer":
        footerLinks = "Shortcut links: "
        footerLinks = footerLinks + "<A HREF='doAction?discover'>Scan for New Lights</A> | "
        footerLinks = footerLinks + "<A HREF='doAction?list'>List Currently Available Lights and Custom Presets</A> | "
        footerLinks = footerLinks + "<A HREF='doAction?list_animations'>Animations</A>"
        self.wfile.write(bytes("<CENTER><HR>" + footerLinks + "<HR></CENTER>\n", "utf-8"))

        if theSection == "quicklinks-timer": # write the "This page will refresh..." timer
            self.wfile.write(bytes("<CENTER><strong><em><span id='refreshDisplay'><BR></span></em></strong></CENTER><HR>\n", "utf-8"))
    elif theSection == "htmlendheaders":
        self.wfile.write(bytes("<CENTER><A HREF='https://github.com/poizenjam/NeewerLux/'>NeewerLux " + NEEWERLUX_VERSION + "</A><BR>based on <A HREF='https://github.com/taburineagle/NeewerLite-Python/'>NeewerLite-Python</A> (v0.12d) by Zach Glenwright / originally from <A HREF='https://github.com/keefo/NeewerLite'>NeewerLite</A> by Xu Lian<BR></CENTER>\n", "utf-8"))
        self.wfile.write(bytes("</BODY>\n</HTML>", "utf-8"))

def formatStringForConsole(theString, maxLength):
    if theString == "-": # return a header divider if the string is "="
        return "-" * maxLength
    else:
        if len(theString) == maxLength: # if the string is the max length, then just return the string
            return theString
        if len(theString) < maxLength: # if the string fits in the max length, then add spaces to pad it out
            return theString + " " * (maxLength - len(theString))
        else: # truncate the string, it's too long
            return theString[0:maxLength - 4] + " ..."

def createLightPrefsFolder():
    #CREATE THE light_prefs FOLDER IF IT DOESN'T EXIST
    try:
        os.mkdir(os.path.dirname(os.path.abspath(sys.argv[0])) + os.sep + "light_prefs")
    except FileExistsError:
        pass # the folder already exists, so we don't need to create it

def markPresetsAsSeeded():
    """Record that this install already has its presets, so they are never re-seeded.

    Best effort. A read-only install directory cannot be marked, but it cannot save
    presets either, so there is no reset for the marker to protect.
    """
    if os.path.exists(presetsSeededMarkerFile):
        return

    try:
        createLightPrefsFolder()

        with open(presetsSeededMarkerFile, mode="w", encoding="utf-8") as markerFile:
            markerFile.write("customLights.prefs has been set up for this install.\n"
                             "Delete this file to have the shipped presets restored on the next launch.\n")
    except OSError as e:
        printDebugString("Could not write the preset seeding marker: " + str(e))

def resolveCustomPresetsFile():
    """Decide which preset file to load, seeding the user copy on a genuine first run.

    customLights.prefs is user state and is not tracked in the repository, so the
    shipped preset names and values live in customLights.prefs.default instead.

    Returns the path to load presets from, or None to use the built-in factory
    presets. Absence of the user file is deliberately not enough on its own to mean
    "first run": both the quick-save and the exit handler delete that file when every
    preset is back at its factory value, so treating absence as a first run would
    undo a reset by copying the shipped presets back in. The marker file records
    that seeding has happened once, and after that an absent file is left absent.
    """
    if os.path.exists(customLightPresetsFile):
        # An upgrade over an existing install. Record that this user already has their
        # presets, so that a later reset (which deletes the file) is not mistaken for a
        # first run and answered by copying the shipped presets back in.
        markPresetsAsSeeded()
        return customLightPresetsFile

    if os.path.exists(presetsSeededMarkerFile):
        return None # seeded before and since removed on purpose, so use the factory presets

    if not os.path.exists(defaultLightPresetsFile):
        return None # nothing shipped to seed from

    try:
        createLightPrefsFolder()
        shutil.copyfile(defaultLightPresetsFile, customLightPresetsFile)
        markPresetsAsSeeded()
        printDebugString("Seeded customLights.prefs from the shipped defaults.")
        return customLightPresetsFile
    except OSError as e:
        # Most likely a read-only install directory. The template is still readable,
        # so load the shipped presets straight out of it rather than dropping to the
        # unnamed built-ins, which have different values.
        printDebugString("Could not seed customLights.prefs (" + str(e) + "), loading the shipped defaults read-only.")
        return defaultLightPresetsFile

def loadPrefsFile(globalPrefsFile = ""):
    global findLightsOnStartup, autoConnectToLights, printDebug, maxNumOfAttempts, \
           rememberLightsOnExit, acceptable_HTTP_IPs, customKeys, enableTabsOnLaunch, \
           whiteListedMACs, rememberPresetsOnExit, autoReconnectOnDisconnect, livePreview, hideConsoleOnLaunch, minimizeToTrayOnClose, httpAutoStart, httpPort, cctFallbackMode, enableLogTab, logToFile, globalCCTMin, globalCCTMax

    if globalPrefsFile != "":
        printDebugString("Loading global preferences from file...")

        with open(globalPrefsFile, mode="r", encoding="utf-8") as fileToOpen:
            mainPrefs = fileToOpen.read().splitlines()

        acceptable_arguments = ["findLightsOnStartup", "autoConnectToLights", "printDebug", "maxNumOfAttempts", "rememberLightsOnExit", "acceptableIPs", \
            "SC_turnOffButton", "SC_turnOnButton", "SC_scanCommandButton", "SC_tryConnectButton", "SC_Tab_CCT", "SC_Tab_HSI", "SC_Tab_SCENE", "SC_Tab_PREFS", \
            "SC_Dec_Bri_Small", "SC_Inc_Bri_Small", "SC_Dec_Bri_Large", "SC_Inc_Bri_Large", \
            "SC_Dec_1_Small", "SC_Inc_1_Small", "SC_Dec_2_Small", "SC_Inc_2_Small", "SC_Dec_3_Small", "SC_Inc_3_Small", \
            "SC_Dec_1_Large", "SC_Inc_1_Large", "SC_Dec_2_Large", "SC_Inc_2_Large", "SC_Dec_3_Large", "SC_Inc_3_Large", \
            "enableTabsOnLaunch", "whiteListedMACs", "rememberPresetsOnExit", "autoReconnectOnDisconnect", "hideConsoleOnLaunch", "minimizeToTrayOnClose", "livePreview", "httpAutoStart", "httpPort", "cctFallbackMode", "enableLogTab", "logToFile", "globalCCTMin", "globalCCTMax"]

        # KICK OUT ANY PARAMETERS THAT AREN'T IN THE "ACCEPTABLE ARGUMENTS" LIST ABOVE
        # THIS SECTION OF CODE IS *SLIGHTLY* DIFFERENT THAN THE CLI KICK OUT CODE
        # THIS WAY, WE CAN HAVE COMMENTS IN THE PREFS FILE IF DESIRED
        for a in range(len(mainPrefs) - 1, -1, -1):
            if not any(x in mainPrefs[a] for x in acceptable_arguments): # if the current argument is invalid
                mainPrefs.pop(a) # delete the invalid argument from the list

        # NOW THAT ANY STRAGGLERS ARE OUT, ADD DASHES TO WHAT REMAINS TO PROPERLY PARSE IN THE PARSER
        for a in range(len(mainPrefs)):
            mainPrefs[a] = "--" + mainPrefs[a]
    else:
        mainPrefs = [] # submit an empty list to return the default values for everything

    prefsParser = argparse.ArgumentParser() # parser for preference arguments

    # SET PROGRAM DEFAULTS
    prefsParser.add_argument("--findLightsOnStartup", default=1)
    prefsParser.add_argument("--autoConnectToLights", default=1)
    prefsParser.add_argument("--printDebug", default=1)
    prefsParser.add_argument("--maxNumOfAttempts", default=6)
    prefsParser.add_argument("--rememberLightsOnExit", default=0)
    prefsParser.add_argument("--acceptableIPs", default=["127.0.0.1", "192.168.", "10."])
    prefsParser.add_argument("--whiteListedMACs" , default=[])
    prefsParser.add_argument("--rememberPresetsOnExit", default=1)
    prefsParser.add_argument("--livePreview", default=1)
    prefsParser.add_argument("--autoReconnectOnDisconnect", default=1)
    prefsParser.add_argument("--hideConsoleOnLaunch", default=0)
    prefsParser.add_argument("--minimizeToTrayOnClose", default=1)
    prefsParser.add_argument("--httpAutoStart", default=0)
    prefsParser.add_argument("--httpPort", default=8080)
    prefsParser.add_argument("--cctFallbackMode", default="convert")
    prefsParser.add_argument("--enableLogTab", default=1)
    prefsParser.add_argument("--logToFile", default=0)
    prefsParser.add_argument("--globalCCTMin", default=3200)
    prefsParser.add_argument("--globalCCTMax", default=5600)

    # SHORTCUT KEY CUSTOMIZATIONS
    prefsParser.add_argument("--SC_turnOffButton", default="Ctrl+PgDown") # 0
    prefsParser.add_argument("--SC_turnOnButton", default="Ctrl+PgUp") # 1
    prefsParser.add_argument("--SC_scanCommandButton", default="Ctrl+Shift+S") # 2
    prefsParser.add_argument("--SC_tryConnectButton", default="Ctrl+Shift+C") # 3
    prefsParser.add_argument("--SC_Tab_CCT", default="Alt+1") # 4
    prefsParser.add_argument("--SC_Tab_HSI", default="Alt+2") # 5
    prefsParser.add_argument("--SC_Tab_SCENE", default="Alt+3") # 6
    prefsParser.add_argument("--SC_Tab_PREFS", default="Alt+4") # 7
    prefsParser.add_argument("--SC_Dec_Bri_Small", default="/") # 8
    prefsParser.add_argument("--SC_Inc_Bri_Small", default="*") # 9
    prefsParser.add_argument("--SC_Dec_Bri_Large", default="Ctrl+/") # 10
    prefsParser.add_argument("--SC_Inc_Bri_Large", default="Ctrl+*") # 11
    prefsParser.add_argument("--SC_Dec_1_Small", default="7") # 12
    prefsParser.add_argument("--SC_Inc_1_Small", default="9") # 13
    prefsParser.add_argument("--SC_Dec_2_Small", default="4") # 14
    prefsParser.add_argument("--SC_Inc_2_Small", default="6") # 15
    prefsParser.add_argument("--SC_Dec_3_Small", default="1") # 16
    prefsParser.add_argument("--SC_Inc_3_Small", default="3") # 17
    prefsParser.add_argument("--SC_Dec_1_Large", default="Ctrl+7") # 18
    prefsParser.add_argument("--SC_Inc_1_Large", default="Ctrl+9") # 19
    prefsParser.add_argument("--SC_Dec_2_Large", default="Ctrl+4") # 20
    prefsParser.add_argument("--SC_Inc_2_Large", default="Ctrl+6") # 21
    prefsParser.add_argument("--SC_Dec_3_Large", default="Ctrl+1") # 22
    prefsParser.add_argument("--SC_Inc_3_Large", default="Ctrl+3") # 23

    # "HIDDEN" DEBUG OPTIONS - oooooh!
    # THESE ARE OPTIONS THAT HELP DEBUG THINGS, BUT AREN'T REALLY USEFUL FOR NORMAL OPERATION
    # enableTabsOnLaunch SHOWS ALL TABS ACTIVE (INSTEAD OF DISABLING THEM) ON LAUNCH SO EVEN WITHOUT A LIGHT, A BYTESTRING CAN BE CALCULATED
    prefsParser.add_argument("--enableTabsOnLaunch", default=0)

    mainPrefs = prefsParser.parse_args(mainPrefs)

    # SET GLOBAL VALUES BASED ON PREFERENCES
    findLightsOnStartup = bool(int(mainPrefs.findLightsOnStartup)) # whether or not to scan for lights on launch
    autoConnectToLights = bool(int(mainPrefs.autoConnectToLights)) # whether or not to connect to lights when found
    printDebug = bool(int(mainPrefs.printDebug)) # whether or not to display debug messages in the console
    maxNumOfAttempts = int(mainPrefs.maxNumOfAttempts) # maximum number of attempts before failing out
    rememberLightsOnExit = bool(int(mainPrefs.rememberLightsOnExit)) # whether or not to remember light mode/settings when quitting out
    rememberPresetsOnExit = bool(int(mainPrefs.rememberPresetsOnExit)) # whether or not to remember the custom presets when quitting out
    livePreview = bool(int(mainPrefs.livePreview)) # whether sliders send in real-time
    autoReconnectOnDisconnect = bool(int(mainPrefs.autoReconnectOnDisconnect)) # whether or not to auto-reconnect after disconnection
    hideConsoleOnLaunch = bool(int(mainPrefs.hideConsoleOnLaunch)) # whether to auto-hide the console window on GUI startup
    minimizeToTrayOnClose = bool(int(mainPrefs.minimizeToTrayOnClose))
    httpAutoStart = bool(int(mainPrefs.httpAutoStart))
    try:
        httpPort = int(mainPrefs.httpPort)
        if not 1024 <= httpPort <= 65535:
            httpPort = 8080
    except (ValueError, TypeError):
        httpPort = 8080
    cctFallbackMode = mainPrefs.cctFallbackMode if mainPrefs.cctFallbackMode in ("convert", "ignore") else "convert"
    enableLogTab = bool(int(mainPrefs.enableLogTab))
    logToFile = bool(int(mainPrefs.logToFile))
    globalCCTMin = int(mainPrefs.globalCCTMin)
    globalCCTMax = int(mainPrefs.globalCCTMax) # whether closing the window minimizes to tray or quits

    if type(mainPrefs.acceptableIPs) is not list: # we have a string in the return, so we need to post-process it
        acceptable_HTTP_IPs = mainPrefs.acceptableIPs.replace(" ", "").split(";") # split the IP addresses into a list for acceptable IPs
    else: # the return is already a list (the default list), so return it
        acceptable_HTTP_IPs = mainPrefs.acceptableIPs

    if type(mainPrefs.whiteListedMACs) is not list: # if we've specified MAC addresses to whitelist, add them to the global list
        whiteListedMACs = mainPrefs.whiteListedMACs.replace(" ", "").split(";")

    # RETURN THE CUSTOM KEYBOARD MAPPINGS
    customKeys = [mainPrefs.SC_turnOffButton, mainPrefs.SC_turnOnButton, mainPrefs.SC_scanCommandButton, mainPrefs.SC_tryConnectButton, \
                  mainPrefs.SC_Tab_CCT, mainPrefs.SC_Tab_HSI, mainPrefs.SC_Tab_SCENE, mainPrefs.SC_Tab_PREFS, \
                  mainPrefs.SC_Dec_Bri_Small, mainPrefs.SC_Inc_Bri_Small, mainPrefs.SC_Dec_Bri_Large, mainPrefs.SC_Inc_Bri_Large, \
                  mainPrefs.SC_Dec_1_Small, \
                  mainPrefs.SC_Inc_1_Small, \
                  mainPrefs.SC_Dec_2_Small, \
                  mainPrefs.SC_Inc_2_Small, \
                  mainPrefs.SC_Dec_3_Small, \
                  mainPrefs.SC_Inc_3_Small, \
                  mainPrefs.SC_Dec_1_Large, \
                  mainPrefs.SC_Inc_1_Large, \
                  mainPrefs.SC_Dec_2_Large, \
                  mainPrefs.SC_Inc_2_Large, \
                  mainPrefs.SC_Dec_3_Large, \
                  mainPrefs.SC_Inc_3_Large]
                
    enableTabsOnLaunch = bool(int(mainPrefs.enableTabsOnLaunch))

if __name__ == '__main__':
    # Display the version of NeewerLux we're using
    print("---------------------------------------------------------")
    print("               NeewerLux ver. " + NEEWERLUX_VERSION)
    print("  Cross-platform Neewer LED light control")
    print("  https://github.com/poizenjam/NeewerLux/")
    print("")
    print("  Based on NeewerLite-Python (v0.12d)")
    print("    by Zach Glenwright (@taburineagle)")
    print("  Originally from NeewerLite by Xu Lian (@keefo)")
    print("---------------------------------------------------------")

    singleInstanceLock() # make a lockfile if one doesn't exist yet, and quit out if one does

    if os.path.exists(globalPrefsFile):
        loadPrefsFile(globalPrefsFile) # if a preferences file exists, process it and load the preferences
    else:
        loadPrefsFile() # if it doesn't, then just load the defaults

    customPresetsSource = resolveCustomPresetsFile() # seeds the user copy on a genuine first run

    if customPresetsSource is not None:
        loadCustomPresets(customPresetsSource) # if there's a custom mapping for presets, then load that into memory

    setUpAsyncio() # set up the asyncio loop
    # With no arguments there is no window to show, so run the server: it is the interface.
    cmdReturn = ["HTTP", True]

    if len(sys.argv) > 1: # process switches when any were given
        cmdReturn = processCommands()
        printDebug = cmdReturn[1] # if we use the --quiet option, then don't show debug strings in the console

        if cmdReturn[0] == False: # if we're trying to load the CLI, make sure we aren't already running another version of it
            doAnotherInstanceCheck() # check to see if another instance is running, and if it is, then error out and quit

        # START HTTP SERVER HERE AND SIT IN THIS LOOP UNTIL THE END

    if cmdReturn[0] == "HTTP":
        doAnotherInstanceCheck() # check to see if another instance is running, and if it is, then error out and quit

        # Load animations and light aliases for HTTP access
        loadAllAnimations()
        loadLightAliases()

        # Start a worker thread to process BLE operations (discover, connect, send, etc.)
        # Without this, threadAction commands from processHTMLCommands would never be picked up.
        httpWorker = threading.Thread(target=workerThread, args=(asyncioEventLoop,), name="workerThread", daemon=True)
        httpWorker.start()

        webServer = ThreadingHTTPServer(("", httpPort), NLPythonServer)

        try:
            printDebugString("Starting the HTTP Server on Port " + str(httpPort) + "...")
            printDebugString("-------------------------------------------------------------------------------------")

            # start the HTTP server and wait for requests
            webServer.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            printDebugString("Stopping the HTTP Server...")
            webServer.server_close()

            # Stop the worker thread and disconnect from lights
            printDebugString("Attempting to unlink from lights...")
            threadAction = "quit"
            time.sleep(1)  # give worker thread time to process quit
            try:
                asyncioEventLoop.run_until_complete(parallelAction("disconnect", [-1], False))
            except RuntimeError:
                pass  # event loop may still be in use briefly
           
        printDebugString("Closing the program NOW")
        singleInstanceUnlockandQuit(0) # delete the lock file and quit out

    if cmdReturn[0] == "LIST":
        doAnotherInstanceCheck() # check to see if another instance is running, and if it is, then error out and quit

        print("NeewerLux " + NEEWERLUX_VERSION + " — based on NeewerLite-Python 0.12d by Zach Glenwright / NeewerLite by Xu Lian")
        print("Searching for nearby Neewer lights...")
        asyncioEventLoop.run_until_complete(findDevices())

        if len(availableLights) > 0:
            print()

            if len(availableLights) == 1: # we only found one
                print("We found 1 Neewer light on the last search.")
            else: # we found more than one
                print("We found " + str(len(availableLights)) + " Neewer lights on the last search.")

            print()

            if platform.system() == "Darwin": # if we're on MacOS, then we display the GUID instead of the MAC address
                addressCharsAllowed = 36 # GUID addresses are 36 characters long
                addressString = "GUID (MacOS)"
            else:
                addressCharsAllowed = 17 # MAC addresses are 17 characters long
                addressString = "MAC Address"

            nameCharsAllowed = 79 - addressCharsAllowed # the remaining space is to display the light name

            # PRINT THE HEADERS
            print(formatStringForConsole("Custom Name (Light Type)", nameCharsAllowed) + \
                  " " + \
                  formatStringForConsole(addressString, addressCharsAllowed))

            # PRINT THE SEPARATORS
            print(formatStringForConsole("-", nameCharsAllowed) + " " + formatStringForConsole("-", addressCharsAllowed))

            # PRINT THE LIGHTS
            for a in range(len(availableLights)):
                lightName = availableLights[a][2] + "(" + availableLights[a][0].name + ")"

                print(formatStringForConsole(lightName, nameCharsAllowed) + " " + \
                      formatStringForConsole(availableLights[a][0].address, addressCharsAllowed))

                print(formatStringForConsole(" > RSSI: " + _get_light_rssi(availableLights[a]) + "dBm", nameCharsAllowed))
        else:
            print("We did not find any Neewer lights on the last search.")

        singleInstanceUnlockandQuit(0) # delete the lock file and quit out

    # EVERYTHING BELOW IS THE ONE-SHOT CLI PATH
    if len(sys.argv) > 1:
        printDebugString(" > Show Debug Strings on Console: " + str(cmdReturn[1]))

        printDebugString(" > Mode: " + cmdReturn[3])

        if cmdReturn[3] == "CCT":
            printDebugString(" > Color Temperature: " + str(cmdReturn[4]) + "00K")
            printDebugString(" > Brightness: " + str(cmdReturn[5]))
        elif cmdReturn[3] == "HSI":
            printDebugString(" > Hue: " + str(cmdReturn[4]))
            printDebugString(" > Saturation: " + str(cmdReturn[5]))
            printDebugString(" > Brightness: " + str(cmdReturn[6]))
        elif cmdReturn[3] == "ANM":
            printDebugString(" > Scene: " + str(cmdReturn[4]))
            printDebugString(" > Brightness: " + str(cmdReturn[5]))

        if cmdReturn[0] == False: # if we're not showing the GUI, we need to specify a MAC address
            if cmdReturn[2] != "":
                printDebugString("-------------------------------------------------------------------------------------")
                printDebugString(" > CLI >> MAC Address of light to send command to: " + cmdReturn[2].upper())

                asyncioEventLoop.run_until_complete(connectToOneLight(cmdReturn[2])) # get Bleak object linking to this specific light and getting custom prefs
            else:
                printDebugString("-------------------------------------------------------------------------------------")
                printDebugString(" > CLI >> You did not specify a light to send the command to - use the --light switch")
                printDebugString(" > CLI >> and write either a MAC Address (XX:XX:XX:XX:XX:XX) to a Neewer light or")
                printDebugString(" > CLI >> ALL to send to all available Neewer lights found by Bluetooth")
                printDebugString("-------------------------------------------------------------------------------------")

    # SEND THE COMMAND TO A LIGHT, THEN QUIT OUT
    if cmdReturn[0] is not None:
        if len(cmdReturn) > 1:
            if cmdReturn[3] == "CCT": # calculate CCT bytestring
                calculateByteString(colorMode=cmdReturn[3], temp=cmdReturn[4], brightness=cmdReturn[5])
            elif cmdReturn[3] == "HSI": # calculate HSI bytestring
                calculateByteString(colorMode=cmdReturn[3], HSI_H=cmdReturn[4], HSI_S=cmdReturn[5], HSI_I=cmdReturn[6])
            elif cmdReturn[3] == "ANM": # calculate ANM/SCENE bytestring
                calculateByteString(colorMode=cmdReturn[3], animation=cmdReturn[4], brightness=cmdReturn[5])
            elif cmdReturn[3] == "ON": # turn the light on
                setPowerBytestring("ON")
            elif cmdReturn[3] == "OFF": # turn the light off
                setPowerBytestring("OFF")

        if availableLights != []:
            printDebugString(" > CLI >> Bytestring to send to light:" + updateStatus())

            # CONNECT TO THE LIGHT AND SEND INFORMATION TO IT
            isFinished = False
            numOfAttempts = 1

            while isFinished == False:
                printDebugString("-------------------------------------------------------------------------------------")
                printDebugString(" > CLI >> Attempting to connect to light (attempt " + str(numOfAttempts) + " of " + str(maxNumOfAttempts) + ")")
                printDebugString("-------------------------------------------------------------------------------------")
                isFinished = asyncioEventLoop.run_until_complete(connectToLight(0, False))

                if numOfAttempts < maxNumOfAttempts:
                    numOfAttempts = numOfAttempts + 1
                else:
                    printDebugString("Error connecting to light " + str(maxNumOfAttempts) + " times - quitting out")
                    singleInstanceUnlockandQuit(1) # delete the lock file and quit out

            isFinished = False
            numOfAttempts = 1

            while isFinished == False:
                printDebugString("-------------------------------------------------------------------------------------")
                printDebugString(" > CLI >> Attempting to write to light (attempt " + str(numOfAttempts) + " of " + str(maxNumOfAttempts) + ")")
                printDebugString("-------------------------------------------------------------------------------------")
                isFinished = asyncioEventLoop.run_until_complete(writeToLight(0, False))

                if numOfAttempts < maxNumOfAttempts:
                    numOfAttempts = numOfAttempts + 1
                else:
                    printDebugString("Error writing to light " + str(maxNumOfAttempts) + " times - quitting out")
                    singleInstanceUnlockandQuit(1) # delete the lock file and quit out

            isFinished = False
            numOfAttempts = 1

            while isFinished == False:
                printDebugString("-------------------------------------------------------------------------------------")
                printDebugString(" > CLI >> Attempting to disconnect from light (attempt " + str(numOfAttempts) + " of " + str(maxNumOfAttempts) + ")")
                printDebugString("-------------------------------------------------------------------------------------")
                isFinished = asyncioEventLoop.run_until_complete(disconnectFromLight(0, updateGUI = False))

                if numOfAttempts < maxNumOfAttempts:
                    numOfAttempts = numOfAttempts + 1
                else:
                    printDebugString("Error disconnecting from light " + str(maxNumOfAttempts) + " times - quitting out")
                    singleInstanceUnlockandQuit(1) # delete the lock file and quit out
        else:
            printDebugString("-------------------------------------------------------------------------------------")
            printDebugString(" > CLI >> Calculated bytestring:" + updateStatus())

        singleInstanceUnlockandQuit(0) # delete the lock file and quit out
