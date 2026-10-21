/* SPDX-FileCopyrightText: 2007 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup wm
 *
 * This file contains the splash screen logic (the `WM_OT_splash` operator).
 *
 * - Loads the splash image.
 * - Displaying version information.
 * - Lists New Files (application templates).
 * - Lists Recent files.
 * - Links to web sites.
 */

#include <cstring>

#include "DNA_screen_types.h"
#include "DNA_theme_types.h"
#include "DNA_userdef_types.h"
#include "DNA_windowmanager_types.h"

#include "BLI_listbase.h"
#include "BLI_math_base.h"
#include "BLI_path_utils.hh"
#include "BLI_utildefines.h"

#include "BKE_appdir.hh"
#include "BKE_blender_version.h"
#include "BKE_context.hh"
#include "BKE_preferences.h"

#include "BLT_translation.hh"

#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"

#include "ED_datafiles.h"
#include "ED_screen.hh"

#include "RNA_access.hh"

#include "UI_interface.hh"
#include "UI_interface_icons.hh"
#include "UI_interface_layout.hh"
#include "UI_resources.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "wm.hh"

namespace blender {

/* -------------------------------------------------------------------- */
/** \name Splash Screen
 * \{ */

/**
 * Loopcut: set by the bundled add-on (`WindowManager.splash_modal`) while its first-run setup is
 * on screen. The splash then only closes through its own buttons and dims the window behind it.
 */
static bool splash_modal = false;

void wm_splash_modal_set(const bool modal)
{
  if (splash_modal != modal) {
    splash_modal = modal;
    WM_main_add_notifier(NC_WINDOW, nullptr); /* Draw or clear the backdrop. */
  }
}

bool wm_splash_modal_get()
{
  return splash_modal;
}

/* Loopcut: the setup's brand colors (loopcut.org's palette) over a copy of the user's theme.
 * Only the modal splash block draws with it (#block_theme_override_set): the user's own theme is
 * untouched. */
static bTheme splash_brand_theme;

static void wm_splash_brand_color(uchar dst[4], const uint32_t rgb, const uchar alpha = 255)
{
  dst[0] = uchar(rgb >> 16);
  dst[1] = uchar(rgb >> 8);
  dst[2] = uchar(rgb);
  dst[3] = alpha;
}

static void wm_splash_brand_widget(uiWidgetColors &wcol,
                                   const uint32_t inner,
                                   const uint32_t inner_sel,
                                   const uint32_t text,
                                   const uint32_t text_sel,
                                   const uchar outline_alpha)
{
  wm_splash_brand_color(wcol.inner, inner);
  wm_splash_brand_color(wcol.inner_sel, inner_sel);
  wm_splash_brand_color(wcol.item, inner_sel);
  wm_splash_brand_color(wcol.text, text);
  wm_splash_brand_color(wcol.text_sel, text_sel);
  wm_splash_brand_color(wcol.outline, 0xFFFFFF, outline_alpha);
  wm_splash_brand_color(wcol.outline_sel, 0xFFFFFF, outline_alpha);
  wcol.shaded = 0; /* Flat, like the site. */
}

static const bTheme *wm_splash_brand_theme()
{
  constexpr uint32_t background = 0x121213, surface = 0x17171A, raised = 0x232327;
  constexpr uint32_t raised_hover = 0x2C2C31, deep = 0x0C0C0D, foreground = 0xF2EFE9;
  constexpr uint32_t teal = 0x51A7AE, muted = 0x9C9CA3;
  constexpr uchar border = 20, border_strong = 41; /* White at 8% and 16%. */

  splash_brand_theme = *static_cast<const bTheme *>(U.themes.first);
  ThemeUI &tui = splash_brand_theme.tui;
  wm_splash_brand_widget(
      tui.wcol_menu_back, background, background, foreground, foreground, border);
  wm_splash_brand_widget(tui.wcol_box, surface, surface, foreground, foreground, border);
  /* Operator buttons: the one primary is filled foreground with background text when pressed in
   * (depress), the website's primary; others are raised surfaces. */
  wm_splash_brand_widget(tui.wcol_tool, raised, foreground, foreground, background, border_strong);
  wm_splash_brand_color(tui.wcol_tool.item, raised_hover);
  /* Buttons without emboss are quiet actions: muted, the foreground while hovered. Teal is for
   * links that leave the app (UILayout.link). */
  wm_splash_brand_widget(tui.wcol_regular, raised, raised_hover, muted, foreground, border_strong);
  wm_splash_brand_color(tui.link, teal);
  wm_splash_brand_widget(tui.wcol_option, deep, foreground, foreground, foreground, border_strong);
  wm_splash_brand_color(tui.wcol_option.item, background);
  wm_splash_brand_widget(
      tui.wcol_toggle, raised, foreground, foreground, background, border_strong);
  wm_splash_brand_widget(
      tui.wcol_radio, raised, foreground, foreground, background, border_strong);
  wm_splash_brand_widget(tui.wcol_text, deep, deep, foreground, foreground, border_strong);
  wm_splash_brand_color(tui.wcol_text.item, teal);
  wm_splash_brand_widget(
      tui.wcol_menu, raised, raised_hover, foreground, foreground, border_strong);
  wm_splash_brand_widget(tui.wcol_menu_item, background, raised_hover, foreground, foreground, 0);
  /* Bars (UILayout.progress): muted on a track of white at 8%, borderless and nearly square, so
   * neither a full bar nor a short one reads as a button, a slider or a lone dot. */
  wm_splash_brand_widget(tui.wcol_progress, 0x2A2A2E, muted, muted, muted, 0);
  /* A 2 px radius at 2x: larger corners round a short fill away (its radius is clamped to half its
   * width), which left a 12 px fill looking like a 7 px dot. */
  tui.wcol_progress.roundness = 0.05f;
  return &splash_brand_theme;
}

/** Loopcut: off while the add-on's setup shows its numbered steps, which have a compact header. */
static bool splash_banner = true;

void wm_splash_banner_set(const bool banner)
{
  splash_banner = banner;
}

bool wm_splash_banner_get()
{
  return splash_banner;
}

bool wm_splash_modal_is_open(const bScreen *screen)
{
  return splash_modal && ui::popup_block_is_modal(screen, "splash");
}

static void wm_block_splash_close(bContext *C, ui::Block *block)
{
  wmWindow *win = CTX_wm_window(C);
  popup_block_close(C, win, block);
}

/** Clicking the image closes the splash, except a modal one. */
static void wm_block_splash_image_close(bContext *C, ui::Block *block)
{
  if (!splash_modal) {
    wm_block_splash_close(C, block);
  }
}

static void wm_block_splash_add_label(ui::Block *block, const char *label, int x, int y)
{
  if (!(label && label[0])) {
    return;
  }

  block_emboss_set(block, ui::EmbossType::None);

  ui::Button *but = uiDefBut(
      block, ui::ButtonType::Label, label, 0, y, x, UI_UNIT_Y, nullptr, 0, 0, std::nullopt);
  button_drawflag_disable(but, ui::BUT_TEXT_LEFT);
  button_drawflag_enable(but, ui::BUT_TEXT_RIGHT);

  /* Regardless of theme, this text should always be bright white. */
  uchar color[4] = {255, 255, 255, 255};
  button_color_set(but, color);

  block_emboss_set(block, ui::EmbossType::Emboss);
}

#ifndef WITH_HEADLESS
static void wm_block_splash_image_roundcorners_add(ImBuf *ibuf)
{
  uchar *rct = ibuf->byte_data_for_write();
  if (!rct) {
    return;
  }

  bTheme *btheme = ui::theme::theme_get();
  const float roundness = btheme->tui.wcol_menu_back.roundness * UI_SCALE_FAC;
  const int size = roundness * 20;

  if (size < ibuf->x && size < ibuf->y) {
    /* Y-axis initial offset. */
    rct += 4 * (ibuf->y - size) * ibuf->x;

    for (int y = 0; y < size; y++) {
      for (int x = 0; x < size; x++, rct += 4) {
        const float pixel = 1.0 / size;
        const float u = pixel * x;
        const float v = pixel * y;
        const float distance = sqrt(u * u + v * v);

        /* Pointer offset to the alpha value of pixel. */
        /* NOTE: the left corner is flipped in the X-axis. */
        const int offset_l = 4 * (size - x - x - 1) + 3;
        const int offset_r = 4 * (ibuf->x - size) + 3;

        if (distance > 1.0) {
          rct[offset_l] = 0;
          rct[offset_r] = 0;
        }
        else {
          /* Create a single pixel wide transition for anti-aliasing.
           * Invert the distance and map its range [0, 1] to [0, pixel]. */
          const float fac = (1.0 - distance) * size;

          if (fac > 1.0) {
            continue;
          }

          const uchar alpha = unit_float_to_uchar_clamp(fac);
          rct[offset_l] = alpha;
          rct[offset_r] = alpha;
        }
      }

      /* X-axis offset to the next row. */
      rct += 4 * (ibuf->x - size);
    }
  }
}
#endif /* !WITH_HEADLESS */

static ImBuf *wm_block_splash_image(int width, int *r_height)
{
  ImBuf *ibuf = nullptr;
  int height = 0;
#ifndef WITH_HEADLESS
  if (U.app_template[0] != '\0') {
    char splash_filepath[FILE_MAX];
    char template_directory[FILE_MAX];
    if (BKE_appdir_app_template_id_search(
            U.app_template, template_directory, sizeof(template_directory)))
    {
      BLI_path_join(splash_filepath, sizeof(splash_filepath), template_directory, "splash.png");
      ibuf = IMB_load_image_from_filepath(splash_filepath, ImBufFlags::ByteData);
    }
  }

  if (ibuf == nullptr) {
    const char *custom_splash_path = BLI_getenv("BLENDER_CUSTOM_SPLASH");
    if (custom_splash_path) {
      ibuf = IMB_load_image_from_filepath(custom_splash_path, ImBufFlags::ByteData);
    }
  }

  if (ibuf == nullptr) {
    const uchar *splash_data = reinterpret_cast<const uchar *>(datatoc_splash_png);
    size_t splash_data_size = datatoc_splash_png_size;
    ibuf = IMB_load_image_from_memory(
        splash_data, splash_data_size, ImBufFlags::ByteData, "<splash screen>");
  }

  if (ibuf) {
    ibuf->color_mode = ImColorMode::RGBA; /* The image might not have an alpha channel. */
    height = (width * ibuf->y) / ibuf->x;
    if (width != ibuf->x || height != ibuf->y) {
      IMB_scale(ibuf, width, height, IMBScaleFilter::Box, false);
    }

    wm_block_splash_image_roundcorners_add(ibuf);
    IMB_premultiply_alpha(ibuf);
  }

#else
  UNUSED_VARS(width);
#endif
  *r_height = height;
  return ibuf;
}

static ImBuf *wm_block_splash_banner_image(int *r_width,
                                           int *r_height,
                                           int max_width,
                                           int max_height)
{
  ImBuf *ibuf = nullptr;
  int height = 0;
  int width = max_width;
#ifndef WITH_HEADLESS

  const char *custom_splash_path = BLI_getenv("BLENDER_CUSTOM_SPLASH_BANNER");
  if (custom_splash_path) {
    ibuf = IMB_load_image_from_filepath(custom_splash_path, ImBufFlags::ByteData);
  }

  if (!ibuf) {
    return nullptr;
  }

  ibuf->color_mode = ImColorMode::RGBA; /* The image might not have an alpha channel. */

  width = ibuf->x;
  height = ibuf->y;
  if (width > 0 && height > 0 && (width > max_width || height > max_height)) {
    const float splash_ratio = max_width / float(max_height);
    const float banner_ratio = ibuf->x / float(ibuf->y);

    if (banner_ratio > splash_ratio) {
      /* The banner is wider than the splash image. */
      width = max_width;
      height = max_width / banner_ratio;
    }
    else if (banner_ratio < splash_ratio) {
      /* The banner is taller than the splash image. */
      height = max_height;
      width = max_height * banner_ratio;
    }
    else {
      width = max_width;
      height = max_height;
    }
    if (width != ibuf->x || height != ibuf->y) {
      IMB_scale(ibuf, width, height, IMBScaleFilter::Box, false);
    }
  }

  IMB_premultiply_alpha(ibuf);

#else
  UNUSED_VARS(max_height);
#endif
  *r_height = height;
  *r_width = width;
  return ibuf;
}

/**
 * Close the splash when opening a file-selector.
 */
static void wm_block_splash_close_on_fileselect(bContext *C, void *arg1, void * /*arg2*/)
{
  wmWindow *win = CTX_wm_window(C);
  if (!win) {
    return;
  }

  /* Check for the event as this will run before the new window/area has been created. */
  bool has_fileselect = false;
  for (const wmEvent &event : win->runtime->event_queue) {
    if (event.type == EVT_FILESELECT) {
      has_fileselect = true;
      break;
    }
  }

  if (has_fileselect) {
    wm_block_splash_close(C, static_cast<ui::Block *>(arg1));
  }
}

#if defined(__APPLE__)
/* Check if Blender is running under Rosetta for the purpose of displaying a splash screen warning.
 * From Apple's WWDC 2020 Session - Explore the new system architecture of Apple Silicon Macs.
 * Time code: 14:31 - https://developer.apple.com/videos/play/wwdc2020/10686/ */

#  include <sys/sysctl.h>

static int is_using_macos_rosetta()
{
  int ret = 0;
  size_t size = sizeof(ret);

  if (sysctlbyname("sysctl.proc_translated", &ret, &size, nullptr, 0) != -1) {
    return ret;
  }
  /* If "sysctl.proc_translated" is not present then must be native. */
  if (errno == ENOENT) {
    return 0;
  }
  return -1;
}
#endif /* __APPLE__ */

/** The splash image with the version on it, and the optional banner over it. */
static void wm_block_splash_add_images(ui::Block *block, const int splash_width)
{
  int splash_height;

  /* Would be nice to support caching this, so it only has to be re-read (and likely resized) on
   * first draw or if the image changed. */
  ImBuf *ibuf = wm_block_splash_image(splash_width, &splash_height);
  /* This should never happen, if it does - don't crash. */
  if (LIKELY(ibuf)) {
    ui::Button *but = uiDefButImage(
        block, ibuf, 0, 0.5f * U.widget_unit, splash_width, splash_height, nullptr);

    button_func_set(but, [block](bContext &C) { wm_block_splash_image_close(&C, block); });

    if (!splash_modal) { /* Loopcut: the setup's welcome is not about Blender's version. */
      wm_block_splash_add_label(block,
                                BKE_blender_version_string(),
                                splash_width - 8.0 * UI_SCALE_FAC,
                                splash_height - 13.0 * UI_SCALE_FAC);
    }
  }

  /* Banner image passed through the environment, to overlay on the splash and
   * indicate a custom Blender version. Transparency can be used. To replace the
   * full splash screen, see BLENDER_CUSTOM_SPLASH. */
  int banner_width = 0;
  int banner_height = 0;
  ImBuf *bannerbuf = wm_block_splash_banner_image(
      &banner_width, &banner_height, splash_width, splash_height);
  if (bannerbuf) {
    ui::Button *banner_but = uiDefButImage(
        block, bannerbuf, 0, 0.5f * U.widget_unit, banner_width, banner_height, nullptr);

    button_func_set(banner_but, [block](bContext &C) { wm_block_splash_image_close(&C, block); });
  }
}

static ui::Block *wm_block_splash_create(bContext *C, ARegion *region, void * /*arg*/)
{
  const uiStyle *style = ui::style_get_dpi();

  ui::Block *block = block_begin(C, region, "splash", ui::EmbossType::Emboss);

  /* Note on #BLOCK_NO_WIN_CLIP, the window size is not always synchronized
   * with the OS when the splash shows, window clipping in this case gives
   * ugly results and clipping the splash isn't useful anyway, just disable it #32938. */
  block_flag_enable(block, ui::BLOCK_LOOP | ui::BLOCK_KEEP_OPEN | ui::BLOCK_NO_WIN_CLIP);
  block_theme_style_set(block, ui::BLOCK_THEME_STYLE_POPUP);

  int splash_width = style->widget.points * 45 * UI_SCALE_FAC;
  CLAMP_MAX(splash_width, WM_window_native_pixel_x(CTX_wm_window(C)) * 0.7f);

  const int layout_margin_x = UI_SCALE_FAC * 26;
  ui::Layout &layout = ui::block_layout(block,
                                        ui::LayoutDirection::Vertical,
                                        ui::LayoutType::Panel,
                                        layout_margin_x,
                                        0,
                                        splash_width - (layout_margin_x * 2),
                                        UI_SCALE_FAC * 110,
                                        0,
                                        style);

  MenuType *mt;

  /* Draw setup screen if no preferences have been saved yet. */
  if (!bke::preferences::exists()) {
    mt = WM_menutype_find("WM_MT_splash_quick_setup", true);

    /* The #BLOCK_QUICK_SETUP flag prevents the button text from being left-aligned,
     * as it is for all menus due to the #BLOCK_LOOP flag, see in #ui_def_but. */
    block_flag_enable(block, ui::BLOCK_QUICK_SETUP);
  }
  else {
    mt = WM_menutype_find("WM_MT_splash", true);
  }

  block_func_set(block, wm_block_splash_close_on_fileselect, block, nullptr);

  /* Loopcut: the setup centers its button text, as the site does (the flag is read as buttons are
   * made). A draw that just turned the setup on or off is rebuilt once, to match. */
  const bool was_modal = splash_modal;
  if (splash_modal) {
    block_flag_enable(block, ui::BLOCK_QUICK_SETUP);
  }

  if (mt) {
    ui::menutype_draw(C, mt, &layout);
  }
  if (splash_modal != was_modal) {
    ED_region_tag_refresh_ui(region);
  }

  /* After drawing: the add-on's draw decides whether this step is modal, and whether it shows
   * the image (placed above the layout, so outside it). */
  if (splash_modal) {
    block_flag_enable(block, ui::BLOCK_MODAL);
    ui::block_theme_override_set(block, wm_splash_brand_theme());
  }
  if (splash_banner) {
    ui::block_layout_set_current(block, nullptr);
    wm_block_splash_add_images(block, splash_width);
    ui::block_layout_set_current(block, &layout);
  }

/* Displays a warning if blender is being emulated via Rosetta (macOS) or XTA (Windows) */
#if defined(__APPLE__) || defined(_M_X64)
#  if defined(__APPLE__)
  if (is_using_macos_rosetta() > 0)
#  elif defined(_M_X64)
  const char *proc_id = BLI_getenv("PROCESSOR_IDENTIFIER");
  if (proc_id && strncmp(proc_id, "ARM", 3) == 0)
#  endif
  {
    layout.separator(2.0f, ui::LayoutSeparatorType::Line);

    ui::Layout &split = layout.split(0.725, true);
    ui::Layout &row1 = split.row(true);
    ui::Layout &row2 = split.row(true);

    row1.label(RPT_("Intel binary detected. Expect reduced performance."), ICON_ERROR);

    PointerRNA op_ptr = row2.op("WM_OT_url_open",
                                CTX_IFACE_(BLT_I18NCONTEXT_OPERATOR_DEFAULT, "Learn More"),
                                ICON_URL,
                                wm::OpCallContext::InvokeDefault,
                                UI_ITEM_NONE);
#  if defined(__APPLE__)
    RNA_string_set(
        &op_ptr,
        "url",
        "https://docs.blender.org/manual/en/latest/getting_started/installing/macos.html");
#  elif defined(_M_X64)
    RNA_string_set(
        &op_ptr,
        "url",
        "https://docs.blender.org/manual/en/latest/getting_started/installing/windows.html");
#  endif

    layout.separator();
  }
#endif

  /* Without the image, which spans the width, the content's own margin goes all around it. */
  block_bounds_set_centered(block, splash_banner ? 0 : layout_margin_x);

  return block;
}

static wmOperatorStatus wm_splash_invoke(bContext *C,
                                         wmOperator * /*op*/,
                                         const wmEvent * /*event*/)
{
  ui::popup_block_invoke(C, wm_block_splash_create, nullptr, nullptr);

  return OPERATOR_FINISHED;
}

void WM_OT_splash(wmOperatorType *ot)
{
  ot->name = "Splash Screen";
  ot->idname = "WM_OT_splash";
  ot->description = "Open the splash screen with release info";

  ot->invoke = wm_splash_invoke;
  ot->poll = WM_operator_winactive;
}

static wmOperatorStatus wm_splash_close_exec(bContext *C, wmOperator * /*op*/)
{
  wmWindowManager *wm = CTX_wm_manager(C);
  bool closed = false;
  for (wmWindow &win : wm->windows) {
    closed |= ui::popup_block_close_by_name(C, &win, "splash");
  }
  return closed ? OPERATOR_FINISHED : OPERATOR_CANCELLED;
}

void WM_OT_splash_close(wmOperatorType *ot)
{
  ot->name = "Close Splash Screen";
  ot->idname = "WM_OT_splash_close";
  ot->description = "Close the splash screen, a modal one included";

  ot->exec = wm_splash_close_exec;
  ot->flag = OPTYPE_INTERNAL;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Splash Screen: About
 * \{ */

static ui::Block *wm_block_about_create(bContext *C, ARegion *region, void * /*arg*/)
{
  const uiStyle *style = ui::style_get_dpi();
  const int dialog_width = style->widget.points * 42 * UI_SCALE_FAC;

  ui::Block *block = block_begin(C, region, "about", ui::EmbossType::Emboss);

  block_flag_enable(block, ui::BLOCK_KEEP_OPEN | ui::BLOCK_LOOP | ui::BLOCK_NO_WIN_CLIP);
  block_theme_style_set(block, ui::BLOCK_THEME_STYLE_POPUP);

  ui::Layout &layout = ui::block_layout(block,
                                        ui::LayoutDirection::Vertical,
                                        ui::LayoutType::Panel,
                                        0,
                                        0,
                                        dialog_width,
                                        0,
                                        0,
                                        style);

/* Blender logo. */
#ifndef WITH_HEADLESS
  constexpr bool show_color = false;
  const float size = 0.2f * dialog_width;

  ImBuf *ibuf = ui::svg_icon_bitmap(ICON_BLENDER_LOGO_LARGE, size, show_color);

  if (ibuf) {
    bTheme *btheme = ui::theme::theme_get();
    const uchar *color = btheme->tui.wcol_menu_back.text_sel;

    /* The top margin. */
    layout.row(false).separator(0.2f);

    /* The logo image. */
    layout.row(false).alignment_set(ui::LayoutAlign::Left);
    uiDefButImage(block, ibuf, 0, U.widget_unit, ibuf->x, ibuf->y, show_color ? nullptr : color);

    /* Padding below the logo. */
    layout.row(false).separator(2.7f);
  }
#endif /* !WITH_HEADLESS */

  ui::Layout &col = layout.column(true);

  uiItemL_ex(&col, IFACE_("Blender"), ICON_NONE, true, false);

  MenuType *mt = WM_menutype_find("WM_MT_splash_about", true);
  if (mt) {
    ui::menutype_draw(C, mt, &col);
  }

  block_bounds_set_centered(block, 22 * UI_SCALE_FAC);

  return block;
}

static wmOperatorStatus wm_splash_about_invoke(bContext *C,
                                               wmOperator * /*op*/,
                                               const wmEvent * /*event*/)
{
  ui::popup_block_invoke(C, wm_block_about_create, nullptr, nullptr);

  return OPERATOR_FINISHED;
}

void WM_OT_splash_about(wmOperatorType *ot)
{
  ot->name = "About Blender";
  ot->idname = "WM_OT_splash_about";
  ot->description = "Open a window with information about Blender";

  ot->invoke = wm_splash_about_invoke;
  ot->poll = WM_operator_winactive;
}

/** \} */

}  // namespace blender
