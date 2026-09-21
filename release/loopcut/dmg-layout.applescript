-- Lays out the Loopcut disk image's window: run by CPack (CPACK_DMG_DS_STORE_SETUP_SCRIPT) with the
-- volume name as its argument while the image is mounted read-write. Positions are points in a
-- 660 x 480 window and match the drawing in dmg-background.png (make_assets.py): the app at
-- (170, 150), Applications at (490, 150), the readme at (80, 330). Finder snaps them to its grid
-- (read back after a remount they are 184, 504 and 94); the drawing allows for that.
-- Finder applies a window's saved size on open and only keeps the bounds it is given once the
-- window has settled, so the bounds are set twice around a close and reopen (as create-dmg does).
on run argv
  set volumeName to item 1 of argv
  tell application "Finder"
    tell disk volumeName
      open
      set current view of container window to icon view
      set toolbar visible of container window to false
      set statusbar visible of container window to false
      set pathbar visible of container window to false
      set bounds of container window to {200, 120, 860, 600}
      set viewOptions to the icon view options of container window
      set arrangement of viewOptions to not arranged
      set icon size of viewOptions to 96
      set text size of viewOptions to 12
      set background picture of viewOptions to file ".background:background.png"
      set position of item "Loopcut.app" of container window to {170, 150}
      set position of item "Applications" of container window to {490, 150}
      set position of item "Read me first.txt" of container window to {80, 330}
      close
      open
      delay 1
      set bounds of container window to {200, 120, 860, 600}
      set position of item "Loopcut.app" of container window to {170, 150}
      set position of item "Applications" of container window to {490, 150}
      set position of item "Read me first.txt" of container window to {80, 330}
      update without registering applications
      delay 3
      close
    end tell
  end tell
end run
