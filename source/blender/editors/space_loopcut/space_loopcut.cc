/* SPDX-FileCopyrightText: 2026 Loopcut Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup sploopcut
 *
 * The Loopcut editor is a shell for the bundled `loopcut` Python add-on, which draws the whole
 * panel from a `SpaceLoopcut.draw_handler_add(..., 'WINDOW', 'POST_PIXEL')` callback and takes
 * input through operators in the "Loopcut" keymap. Keep logic out of this file.
 */

#include "DNA_space_types.h"

#include "MEM_guardedalloc.h"

#include "BLI_listbase.h"
#include "BLI_string_utf8.h"
#include "BLI_utildefines.h"

#include "BKE_screen.hh"

#include "ED_screen.hh"
#include "ED_space_api.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "UI_resources.hh"

#include "BLO_read_write.hh"

namespace blender {

static SpaceLink *loopcut_create(const ScrArea * /*area*/, const Scene * /*scene*/)
{
  SpaceLoopcut *sloopcut = MEM_new<SpaceLoopcut>("initloopcut");
  sloopcut->spacetype = SPACE_LOOPCUT;

  /* Header: only holds the editor-type menu, and the panel draws its own title bar, so it starts
   * hidden. The arrow Blender shows for a hidden header brings it back. */
  ARegion *region = BKE_area_region_new();
  BLI_addtail(&sloopcut->regionbase, region);
  region->regiontype = RGN_TYPE_HEADER;
  region->alignment = (U.uiflag & USER_HEADER_BOTTOM) ? RGN_ALIGN_BOTTOM : RGN_ALIGN_TOP;
  region->flag |= RGN_FLAG_HIDDEN;

  /* Main region. */
  region = BKE_area_region_new();
  BLI_addtail(&sloopcut->regionbase, region);
  region->regiontype = RGN_TYPE_WINDOW;

  return reinterpret_cast<SpaceLink *>(sloopcut);
}

/* Doesn't free the space-link itself. */
static void loopcut_free(SpaceLink * /*sl*/) {}

static void loopcut_init(wmWindowManager * /*wm*/, ScrArea * /*area*/) {}

static SpaceLink *loopcut_duplicate(SpaceLink *sl)
{
  SpaceLoopcut *sloopcutn = MEM_dupalloc(reinterpret_cast<SpaceLoopcut *>(sl));
  return reinterpret_cast<SpaceLink *>(sloopcutn);
}

static void loopcut_keymap(wmKeyConfig *keyconf)
{
  WM_keymap_ensure(keyconf, "Loopcut", SPACE_LOOPCUT, RGN_TYPE_WINDOW);
}

static void loopcut_main_region_init(wmWindowManager *wm, ARegion *region)
{
  wmKeyMap *keymap = WM_keymap_ensure(
      wm->runtime->defaultconf, "Loopcut", SPACE_LOOPCUT, RGN_TYPE_WINDOW);
  WM_event_add_keymap_handler(&region->runtime->handlers, keymap);
}

static void loopcut_main_region_draw(const bContext * /*C*/, ARegion * /*region*/)
{
  /* The add-on's POST_PIXEL callback runs right after this, from #ED_region_do_draw. */
  ui::theme::frame_buffer_clear(TH_BACK);
}

static void loopcut_main_region_listener(const wmRegionListenerParams *params)
{
  /* The panel shows what is selected and which file is open, so it follows both. Everything
   * else it draws is its own state, and the add-on tags the redraw for that itself. */
  const wmNotifier *wmn = params->notifier;
  switch (wmn->category) {
    case NC_SCENE:
      if (ELEM(wmn->data, ND_OB_ACTIVE, ND_OB_SELECT, ND_MODE)) {
        ED_region_tag_redraw(params->region);
      }
      break;
    case NC_WM:
      if (wmn->data == ND_FILEREAD) {
        ED_region_tag_redraw(params->region);
      }
      break;
    default:
      break;
  }
}

static void loopcut_header_region_init(wmWindowManager * /*wm*/, ARegion *region)
{
  ED_region_header_init(region);
}

static void loopcut_header_region_draw(const bContext *C, ARegion *region)
{
  ED_region_header(C, region);
}

static void loopcut_space_blend_write(BlendWriter *writer, SpaceLink *sl)
{
  writer->write_struct_cast<SpaceLoopcut>(sl);
}

void ED_spacetype_loopcut()
{
  std::unique_ptr<SpaceType> st = std::make_unique<SpaceType>();
  ARegionType *art;

  st->spaceid = SPACE_LOOPCUT;
  STRNCPY_UTF8(st->name, "Loopcut");

  st->create = loopcut_create;
  st->free = loopcut_free;
  st->init = loopcut_init;
  st->duplicate = loopcut_duplicate;
  st->keymap = loopcut_keymap;
  st->blend_write = loopcut_space_blend_write;

  /* regions: main window */
  art = MEM_new_zeroed<ARegionType>("spacetype loopcut region");
  art->regionid = RGN_TYPE_WINDOW;
  art->init = loopcut_main_region_init;
  art->draw = loopcut_main_region_draw;
  art->listener = loopcut_main_region_listener;
  BLI_addhead(&st->regiontypes, art);

  /* regions: header */
  art = MEM_new_zeroed<ARegionType>("spacetype loopcut region");
  art->regionid = RGN_TYPE_HEADER;
  art->prefsizey = HEADERY;
  art->keymapflag = ED_KEYMAP_UI | ED_KEYMAP_VIEW2D | ED_KEYMAP_HEADER;
  art->init = loopcut_header_region_init;
  art->draw = loopcut_header_region_draw;
  BLI_addhead(&st->regiontypes, art);

  BKE_spacetype_register(std::move(st));
}

}  // namespace blender
