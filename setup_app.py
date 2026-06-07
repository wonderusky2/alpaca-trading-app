"""
setup_app.py — py2app configuration for Alpaca Paper Trader menu bar app.
Build with: python setup_app.py py2app
"""
from setuptools import setup

APP = ["app.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": None,
    "plist": {
        "CFBundleName":               "Alpaca Paper Trader",
        "CFBundleDisplayName":        "Alpaca Paper Trader",
        "CFBundleIdentifier":         "com.johnshelest.robinhoodtrader.app",
        "CFBundleVersion":            "1.0.0",
        "CFBundleShortVersionString": "1.0",
        "LSUIElement":                True,
        "NSHighResolutionCapable":    True,
        "LSMinimumSystemVersion":     "12.0",
    },
    "packages": [
        "rumps",
        "zoneinfo",
    ],
    "includes": [
        "AppKit",
        "Foundation",
        "objc",
        "json",
        "pathlib",
        "threading",
        "subprocess",
        "shutil",
        "shlex",
    ],
    "excludes": [
        "tkinter",
        "matplotlib",
        "scipy",
        "numpy",
        "pandas",
        "yfinance",
        "filelock",
        "requests",
        "google",
    ],
}

setup(
    app=APP,
    name="Alpaca Paper Trader",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
