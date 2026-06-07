#!/usr/bin/env python3
"""Generate AlpacaAgent.xcodeproj from the Swift source files."""
import os, uuid, shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.join(ROOT, "AlpacaAgent")
XCODEPROJ = os.path.join(ROOT, "AlpacaAgent.xcodeproj")

def uid():
    return uuid.uuid4().hex[:24].upper()

# ── UUIDs ────────────────────────────────────────────────────────────────────
PROJ_ID        = uid()
TARGET_ID      = uid()
BUILD_CFG_LIST_PROJ = uid()
BUILD_CFG_LIST_TGT  = uid()
DEBUG_PROJ     = uid()
RELEASE_PROJ   = uid()
DEBUG_TGT      = uid()
RELEASE_TGT    = uid()
SOURCES_PHASE  = uid()
RESOURCES_PHASE = uid()
FRAMEWORKS_PHASE = uid()
MAIN_GROUP     = uid()
PRODUCTS_GROUP = uid()
SOURCES_GROUP  = uid()
ASSETS_GROUP   = uid()
APP_FILE_REF   = uid()

# Source files
SWIFT_FILES = [
    "AlpacaAgentApp.swift",
    "ContentView.swift",
    "Models.swift",
    "AgentViewModel.swift",
    "Config.swift",
]
FILE_REFS   = {f: uid() for f in SWIFT_FILES}
BUILD_FILES = {f: uid() for f in SWIFT_FILES}
INFO_REF    = uid()
ASSETS_REF  = uid()
ASSETS_BUILD = uid()

BUNDLE_ID = "com.johnshelest.AlpacaAgent"

pbxproj = f"""// !$*UTF8*$!
{{
\tarchiveVersion = 1;
\tclasses = {{
\t}};
\tobjectVersion = 56;
\tobjects = {{

/* Begin PBXBuildFile section */
"""

for f in SWIFT_FILES:
    pbxproj += f'\t\t{BUILD_FILES[f]} /* {f} in Sources */ = {{isa = PBXBuildFile; fileRef = {FILE_REFS[f]} /* {f} */; }};\n'
pbxproj += f'\t\t{ASSETS_BUILD} /* Assets.xcassets in Resources */ = {{isa = PBXBuildFile; fileRef = {ASSETS_REF} /* Assets.xcassets */; }};\n'

pbxproj += """/* End PBXBuildFile section */

/* Begin PBXFileReference section */
"""

pbxproj += f'\t\t{APP_FILE_REF} /* AlpacaAgent.app */ = {{isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = AlpacaAgent.app; sourceTree = BUILT_PRODUCTS_DIR; }};\n'
for f in SWIFT_FILES:
    pbxproj += f'\t\t{FILE_REFS[f]} /* {f} */ = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {f}; sourceTree = "<group>"; }};\n'
pbxproj += f'\t\t{INFO_REF} /* Info.plist */ = {{isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = "<group>"; }};\n'
pbxproj += f'\t\t{ASSETS_REF} /* Assets.xcassets */ = {{isa = PBXFileReference; lastKnownFileType = folder.assetcatalog; path = Assets.xcassets; sourceTree = "<group>"; }};\n'

pbxproj += f"""/* End PBXFileReference section */

/* Begin PBXFrameworksBuildPhase section */
\t\t{FRAMEWORKS_PHASE} /* Frameworks */ = {{
\t\t\tisa = PBXFrameworksBuildPhase;
\t\t\tbuildActionMask = 2147483647;
\t\t\tfiles = (
\t\t\t);
\t\t\trunOnlyForDeploymentPostprocessing = 0;
\t\t}};
/* End PBXFrameworksBuildPhase section */

/* Begin PBXGroup section */
\t\t{MAIN_GROUP} = {{
\t\t\tisa = PBXGroup;
\t\t\tchildren = (
\t\t\t\t{SOURCES_GROUP} /* AlpacaAgent */,
\t\t\t\t{PRODUCTS_GROUP} /* Products */,
\t\t\t);
\t\t\tsourceTree = "<group>";
\t\t}};
\t\t{PRODUCTS_GROUP} /* Products */ = {{
\t\t\tisa = PBXGroup;
\t\t\tchildren = (
\t\t\t\t{APP_FILE_REF} /* AlpacaAgent.app */,
\t\t\t);
\t\t\tname = Products;
\t\t\tsourceTree = "<group>";
\t\t}};
\t\t{SOURCES_GROUP} /* AlpacaAgent */ = {{
\t\t\tisa = PBXGroup;
\t\t\tchildren = (
"""
for f in SWIFT_FILES:
    pbxproj += f'\t\t\t\t{FILE_REFS[f]} /* {f} */,\n'
pbxproj += f'\t\t\t\t{ASSETS_REF} /* Assets.xcassets */,\n'
pbxproj += f'\t\t\t\t{INFO_REF} /* Info.plist */,\n'
pbxproj += f"""\t\t\t);
\t\t\tpath = AlpacaAgent;
\t\t\tsourceTree = "<group>";
\t\t}};
/* End PBXGroup section */

/* Begin PBXNativeTarget section */
\t\t{TARGET_ID} /* AlpacaAgent */ = {{
\t\t\tisa = PBXNativeTarget;
\t\t\tbuildConfigurationList = {BUILD_CFG_LIST_TGT} /* Build configuration list for PBXNativeTarget "AlpacaAgent" */;
\t\t\tbuildPhases = (
\t\t\t\t{SOURCES_PHASE} /* Sources */,
\t\t\t\t{FRAMEWORKS_PHASE} /* Frameworks */,
\t\t\t\t{RESOURCES_PHASE} /* Resources */,
\t\t\t);
\t\t\tbuildRules = (
\t\t\t);
\t\t\tdependencies = (
\t\t\t);
\t\t\tname = AlpacaAgent;
\t\t\tproductName = AlpacaAgent;
\t\t\tproductReference = {APP_FILE_REF} /* AlpacaAgent.app */;
\t\t\tproductType = "com.apple.product-type.application";
\t\t}};
/* End PBXNativeTarget section */

/* Begin PBXProject section */
\t\t{PROJ_ID} /* Project object */ = {{
\t\t\tisa = PBXProject;
\t\t\tattributes = {{
\t\t\t\tBuildIndependentTargetsInParallel = 1;
\t\t\t\tLastSwiftUpdateCheck = 1540;
\t\t\t\tLastUpgradeCheck = 1540;
\t\t\t\tTargetAttributes = {{
\t\t\t\t\t{TARGET_ID} = {{
\t\t\t\t\t\tCreatedOnToolsVersion = 15.4;
\t\t\t\t\t}};
\t\t\t\t}};
\t\t\t}};
\t\t\tbuildConfigurationList = {BUILD_CFG_LIST_PROJ} /* Build configuration list for PBXProject "AlpacaAgent" */;
\t\t\tcompatibilityVersion = "Xcode 14.0";
\t\t\tdevelopmentRegion = en;
\t\t\thasScannedForEncodings = 0;
\t\t\tknownRegions = (
\t\t\t\ten,
\t\t\t\tBase,
\t\t\t);
\t\t\tmainGroup = {MAIN_GROUP};
\t\t\tproductsGroup = {PRODUCTS_GROUP} /* Products */;
\t\t\tprojectDirPath = "";
\t\t\tprojectRoot = "";
\t\t\ttargets = (
\t\t\t\t{TARGET_ID} /* AlpacaAgent */,
\t\t\t);
\t\t}};
/* End PBXProject section */

/* Begin PBXResourcesBuildPhase section */
\t\t{RESOURCES_PHASE} /* Resources */ = {{
\t\t\tisa = PBXResourcesBuildPhase;
\t\t\tbuildActionMask = 2147483647;
\t\t\tfiles = (
\t\t\t\t{ASSETS_BUILD} /* Assets.xcassets in Resources */,
\t\t\t);
\t\t\trunOnlyForDeploymentPostprocessing = 0;
\t\t}};
/* End PBXResourcesBuildPhase section */

/* Begin PBXSourcesBuildPhase section */
\t\t{SOURCES_PHASE} /* Sources */ = {{
\t\t\tisa = PBXSourcesBuildPhase;
\t\t\tbuildActionMask = 2147483647;
\t\t\tfiles = (
"""
for f in SWIFT_FILES:
    pbxproj += f'\t\t\t\t{BUILD_FILES[f]} /* {f} in Sources */,\n'
pbxproj += f"""\t\t\t);
\t\t\trunOnlyForDeploymentPostprocessing = 0;
\t\t}};
/* End PBXSourcesBuildPhase section */

/* Begin XCBuildConfiguration section */
\t\t{DEBUG_PROJ} /* Debug */ = {{
\t\t\tisa = XCBuildConfiguration;
\t\t\tbuildSettings = {{
\t\t\t\tALWAYS_SEARCH_USER_PATHS = NO;
\t\t\t\tCLANG_ANALYZER_NONNULL = YES;
\t\t\t\tCLANG_CXX_LANGUAGE_STANDARD = "gnu++20";
\t\t\t\tCLANG_ENABLE_MODULES = YES;
\t\t\t\tCLANG_ENABLE_OBJC_ARC = YES;
\t\t\t\tCOPY_PHASE_STRIP = NO;
\t\t\t\tDEBUG_INFORMATION_FORMAT = dwarf;
\t\t\t\tENABLE_STRICT_OBJC_MSGSEND = YES;
\t\t\t\tENABLE_TESTABILITY = YES;
\t\t\t\tGCC_C_LANGUAGE_STANDARD = gnu17;
\t\t\t\tGCC_DYNAMIC_NO_PIC = NO;
\t\t\t\tGCC_NO_COMMON_BLOCKS = YES;
\t\t\t\tGCC_OPTIMIZATION_LEVEL = 0;
\t\t\t\tGCC_PREPROCESSOR_DEFINITIONS = (
\t\t\t\t\t"DEBUG=1",
\t\t\t\t\t"$(inherited)",
\t\t\t\t);
\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 17.0;
\t\t\t\tMTL_ENABLE_DEBUG_INFO = INCLUDE_SOURCE;
\t\t\t\tMTL_FAST_MATH = YES;
\t\t\t\tONLY_ACTIVE_ARCH = YES;
\t\t\t\tSDKROOT = iphoneos;
\t\t\t\tSWIFT_ACTIVE_COMPILATION_CONDITIONS = DEBUG;
\t\t\t\tSWIFT_OPTIMIZATION_LEVEL = "-Onone";
\t\t\t}};
\t\t\tname = Debug;
\t\t}};
\t\t{RELEASE_PROJ} /* Release */ = {{
\t\t\tisa = XCBuildConfiguration;
\t\t\tbuildSettings = {{
\t\t\t\tALWAYS_SEARCH_USER_PATHS = NO;
\t\t\t\tCLANG_ANALYZER_NONNULL = YES;
\t\t\t\tCLANG_CXX_LANGUAGE_STANDARD = "gnu++20";
\t\t\t\tCLANG_ENABLE_MODULES = YES;
\t\t\t\tCLANG_ENABLE_OBJC_ARC = YES;
\t\t\t\tCOPY_PHASE_STRIP = NO;
\t\t\t\tDEBUG_INFORMATION_FORMAT = "dwarf-with-dsym";
\t\t\t\tENABLE_NS_ASSERTIONS = NO;
\t\t\t\tENABLE_STRICT_OBJC_MSGSEND = YES;
\t\t\t\tGCC_C_LANGUAGE_STANDARD = gnu17;
\t\t\t\tGCC_NO_COMMON_BLOCKS = YES;
\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 17.0;
\t\t\t\tMTL_ENABLE_DEBUG_INFO = NO;
\t\t\t\tMTL_FAST_MATH = YES;
\t\t\t\tSDKROOT = iphoneos;
\t\t\t\tSWIFT_COMPILATION_MODE = wholemodule;
\t\t\t\tVALIDATE_PRODUCT = YES;
\t\t\t}};
\t\t\tname = Release;
\t\t}};
\t\t{DEBUG_TGT} /* Debug */ = {{
\t\t\tisa = XCBuildConfiguration;
\t\t\tbuildSettings = {{
\t\t\t\tASSTCATA_COMPILER_APPICON_NAME = AppIcon;
\t\t\t\tASSTCATA_COMPILER_GLOBAL_ACCENT_COLOR_NAME = AccentColor;
\t\t\t\tCODE_SIGN_STYLE = Automatic;
\t\t\t\tCURRENT_PROJECT_VERSION = 1;
\t\t\t\tENABLE_PREVIEWS = YES;
\t\t\t\tGENERATE_INFOPLIST_FILE = NO;
\t\t\t\tINFOPLIST_FILE = AlpacaAgent/Info.plist;
\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 17.0;
\t\t\t\tLE_SWIFT_VERSION = 6.0;
\t\t\t\tMARKETING_VERSION = 1.0;
\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = "{BUNDLE_ID}";
\t\t\t\tPRODUCT_NAME = "$(TARGET_NAME)";
\t\t\t\tSDKROOT = iphoneos;
\t\t\t\tSUPPORTED_PLATFORMS = "iphoneos iphonesimulator";
\t\t\t\tSWIFT_VERSION = 6.0;
\t\t\t\tTARGETED_DEVICE_FAMILY = "1,2";
\t\t\t}};
\t\t\tname = Debug;
\t\t}};
\t\t{RELEASE_TGT} /* Release */ = {{
\t\t\tisa = XCBuildConfiguration;
\t\t\tbuildSettings = {{
\t\t\t\tASSTCATA_COMPILER_APPICON_NAME = AppIcon;
\t\t\t\tASSTCATA_COMPILER_GLOBAL_ACCENT_COLOR_NAME = AccentColor;
\t\t\t\tCODE_SIGN_STYLE = Automatic;
\t\t\t\tCURRENT_PROJECT_VERSION = 1;
\t\t\t\tENABLE_PREVIEWS = YES;
\t\t\t\tGENERATE_INFOPLIST_FILE = NO;
\t\t\t\tINFOPLIST_FILE = AlpacaAgent/Info.plist;
\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 17.0;
\t\t\t\tLE_SWIFT_VERSION = 6.0;
\t\t\t\tMARKETING_VERSION = 1.0;
\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = "{BUNDLE_ID}";
\t\t\t\tPRODUCT_NAME = "$(TARGET_NAME)";
\t\t\t\tSDKROOT = iphoneos;
\t\t\t\tSUPPORTED_PLATFORMS = "iphoneos iphonesimulator";
\t\t\t\tSWIFT_VERSION = 6.0;
\t\t\t\tTARGETED_DEVICE_FAMILY = "1,2";
\t\t\t}};
\t\t\tname = Release;
\t\t}};
/* End XCBuildConfiguration section */

/* Begin XCConfigurationList section */
\t\t{BUILD_CFG_LIST_PROJ} /* Build configuration list for PBXProject "AlpacaAgent" */ = {{
\t\t\tisa = XCConfigurationList;
\t\t\tbuildConfigurations = (
\t\t\t\t{DEBUG_PROJ} /* Debug */,
\t\t\t\t{RELEASE_PROJ} /* Release */,
\t\t\t);
\t\t\tdefaultConfigurationIsVisible = 0;
\t\t\tdefaultConfigurationName = Release;
\t\t}};
\t\t{BUILD_CFG_LIST_TGT} /* Build configuration list for PBXNativeTarget "AlpacaAgent" */ = {{
\t\t\tisa = XCConfigurationList;
\t\t\tbuildConfigurations = (
\t\t\t\t{DEBUG_TGT} /* Debug */,
\t\t\t\t{RELEASE_TGT} /* Release */,
\t\t\t);
\t\t\tdefaultConfigurationIsVisible = 0;
\t\t\tdefaultConfigurationName = Release;
\t\t}};
/* End XCConfigurationList section */
\t}};
\trootObject = {PROJ_ID} /* Project object */;
}}
"""

# ── Write project.pbxproj ─────────────────────────────────────────────────────
os.makedirs(XCODEPROJ, exist_ok=True)
with open(os.path.join(XCODEPROJ, "project.pbxproj"), "w") as f:
    f.write(pbxproj)
print("✓ project.pbxproj written")

# ── Assets.xcassets ───────────────────────────────────────────────────────────
assets_dir = os.path.join(PROJ_DIR, "Assets.xcassets")
appicon_dir = os.path.join(assets_dir, "AppIcon.appiconset")
accent_dir  = os.path.join(assets_dir, "AccentColor.colorset")
os.makedirs(appicon_dir, exist_ok=True)
os.makedirs(accent_dir, exist_ok=True)

with open(os.path.join(assets_dir, "Contents.json"), "w") as f:
    f.write('{\n  "info": { "author": "xcode", "version": 1 }\n}\n')

with open(os.path.join(appicon_dir, "Contents.json"), "w") as f:
    f.write('''{
  "images": [
    {"idiom": "universal", "platform": "ios", "size": "1024x1024"}
  ],
  "info": { "author": "xcode", "version": 1 }
}
''')

with open(os.path.join(accent_dir, "Contents.json"), "w") as f:
    f.write('''{
  "colors": [
    {
      "color": {
        "color-space": "srgb",
        "components": { "alpha": "1.000", "blue": "1.000", "green": "0.651", "red": "0.345" }
      },
      "idiom": "universal"
    }
  ],
  "info": { "author": "xcode", "version": 1 }
}
''')

print("✓ Assets.xcassets written")
print(f"\n✅ Done — open: open {XCODEPROJ}")
