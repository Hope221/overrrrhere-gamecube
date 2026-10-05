// Pont GameCube et Wii pour l'app overrrrhere. Reprend la recette d'iCube (DolphinCoreService.mm
// pour le démarrage, EmulationCoordinator.mm pour le lancement d'un jeu), sans JIT (Cached
// Interpreter), sans BIOS (démarrage direct du disque).
// Compilé avec le cœur Dolphin (Source/iOS/Library) par le workflow « Cœur GameCube ».
// SPDX-License-Identifier: GPL-2.0-or-later

#import "GameCube.h"

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>

#include <mach/mach.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "Common/Config/Config.h"
#include "Common/FileUtil.h"
#include "Common/IniFile.h"
#include "Common/MsgHandler.h"
#include "Common/WindowSystemInfo.h"
#include "Core/Boot/Boot.h"
#include "Core/BootManager.h"
#include "Core/Config/GraphicsSettings.h"
#include "Core/Config/MainSettings.h"
#include "Core/Core.h"
#include "Core/Config/WiimoteSettings.h"
#include "Core/HW/GCPad.h"
#include "Core/HW/SI/SI_Device.h"
#include "Core/HW/Wiimote.h"
#include "Core/Host.h"
#include "Core/PowerPC/PowerPC.h"
#include "Core/State.h"
#include "Core/System.h"
#include "DiscIO/Enums.h"
#include "DiscIO/Volume.h"
#include "InputCommon/ControllerEmu/ControllerEmu.h"
#include "InputCommon/ControllerInterface/ControllerInterface.h"
#include "InputCommon/ControllerInterface/iOS/StateManager.h"
#include "InputCommon/InputConfig.h"
#include "UICommon/UICommon.h"
#include "VideoCommon/PerformanceMetrics.h"
#include "VideoCommon/Present.h"
#include "VideoCommon/VideoConfig.h"

#include "HostQueue.h"

// Dolphin modifie la couche Metal depuis ses fils ; UIKit exige le fil principal (comme iCube).
@interface GCMetalLayer : CAMetalLayer
@end

@implementation GCMetalLayer
#define GC_ON_MAIN(code)                                   \
  if ([NSThread isMainThread]) {                           \
    code;                                                  \
  } else {                                                 \
    dispatch_async(dispatch_get_main_queue(), ^{ code; }); \
  }
- (void)setContentsScale:(CGFloat)value { GC_ON_MAIN([super setContentsScale:value]) }
- (void)setFramebufferOnly:(BOOL)value { GC_ON_MAIN([super setFramebufferOnly:value]) }
- (void)setMaximumDrawableCount:(NSUInteger)value { GC_ON_MAIN([super setMaximumDrawableCount:value]) }
- (void)setAllowsNextDrawableTimeout:(BOOL)value { GC_ON_MAIN([super setAllowsNextDrawableTimeout:value]) }
- (void)setDrawableSize:(CGSize)value { GC_ON_MAIN([super setDrawableSize:value]) }
- (void)setPixelFormat:(MTLPixelFormat)value { GC_ON_MAIN([super setPixelFormat:value]) }
@end

static std::atomic<bool> s_ready{false};
static std::atomic<bool> s_loop{false};  // un jeu est lancé (du démarrage à l'arrêt complet)
static GCMetalLayer* s_layer = nil;
static CGSize s_drawable = {0, 0}; // pas CGSizeZero : CoreGraphics n'est pas lié au cœur
static float s_scale = 2.0f;
static dispatch_source_t s_pump = nil;
static std::mutex s_error_mutex;
static std::string s_error;
static dispatch_semaphore_t s_loaded = nil;
static std::atomic<bool> s_load_ok{false};
static std::atomic<int> s_wii_extension{1};  // 0 aucune, 1 Nunchuk, 2 Classic Controller

static void SetError(const std::string& message)
{
  std::lock_guard lock(s_error_mutex);
  s_error = message;
}

// Fenêtres d'alerte de Dolphin : pas de fenêtre dans l'app, le message est gardé pour l'écran d'erreur.
static bool OnAlert(const char* caption, const char* text, bool yes_no, Common::MsgType style)
{
  NSLog(@"[GameCube] %s: %s", caption ? caption : "", text ? text : "");
  if (style == Common::MsgType::Critical || style == Common::MsgType::Warning)
    SetError(text ? text : "");
  return !yes_no;  // question : « non » ; information : continuer
}

// iCube (FastmemManager.m) : la mémoire rapide demande une très grande réserve d'adresses.
static bool FastmemAvailable()
{
  const vm_size_t size = 0x400000000;
  vm_address_t address = 0;
  if (vm_allocate(mach_task_self(), &address, size, VM_FLAGS_ANYWHERE) != KERN_SUCCESS)
    return false;
  vm_deallocate(mach_task_self(), address, size);
  return true;
}

// Réglages imposés : ceux d'iCube pour le mode sans JIT, sans BIOS, sans messages à l'écran.
static void ApplySettings()
{
  const bool fastmem = FastmemAvailable();
  NSLog(@"[GameCube] fastmem %s", fastmem ? "available" : "unavailable");
  Config::SetBase(Config::MAIN_FASTMEM, fastmem);
  Config::SetBase(Config::MAIN_FASTMEM_ARENA, fastmem);
  Config::SetBase(Config::MAIN_CPU_CORE, PowerPC::CPUCore::CachedInterpreter);
  Config::SetBase(Config::MAIN_SKIP_IPL, true);
  Config::SetBase(Config::MAIN_DSP_HLE, true);
  Config::SetBase(Config::MAIN_DSP_THREAD, true);
  Config::SetBase(Config::MAIN_FAST_DISC_SPEED, true);
  Config::SetBase(Config::MAIN_ACCURATE_NANS, false);
  Config::SetBase(Config::MAIN_SYNC_GPU, false);
  Config::SetBase(Config::MAIN_OSD_MESSAGES, false);
  Config::SetBase(Config::MAIN_GFX_BACKEND, std::string("Metal"));
  Config::SetBase(Config::GFX_VERTEX_LOADER_TYPE, VertexLoaderType::NEON);
  Config::SetBase(Config::GFX_HACK_SKIP_EFB_COPY_TO_RAM, true);
  Config::SetBase(Config::GFX_HACK_SKIP_XFB_COPY_TO_RAM, true);
  Config::SetBase(Config::GFX_HACK_IMMEDIATE_XFB, true);
  Config::SetBase(Config::GFX_HACK_VI_SKIP, false);
  Config::SetBase(Config::GFX_HACK_VI_SKIP_MODE, TriState::Auto);
  const int threads = std::max(1, std::min(2, (int)[NSProcessInfo processInfo].processorCount - 1));
  Config::SetBase(Config::GFX_SHADER_COMPILER_THREADS, threads);
  Config::SetBase(Config::GFX_SHADER_PRECOMPILER_THREADS, threads);
  Config::SetBase(Config::GFX_WAIT_FOR_SHADERS_BEFORE_STARTING, true);
  Config::SetBase(Config::GFX_ASYNC_PRESENT, true);
  Config::SetBase(Config::GetInfoForSIDevice(0), SerialInterface::SIDEVICE_GC_CONTROLLER);
  for (int i = 1; i < 4; ++i)
    Config::SetBase(Config::GetInfoForSIDevice(i), SerialInterface::SIDEVICE_NONE);
  // Wii : une seule Wiimote, émulée (jamais de vraie Wiimote en Bluetooth).
  Config::SetBase(Config::GetInfoForWiimoteSource(0), WiimoteSource::Emulated);
  for (int i = 1; i < 4; ++i)
    Config::SetBase(Config::GetInfoForWiimoteSource(i), WiimoteSource::None);
  Config::SetBase(Config::WIIMOTE_BB_SOURCE, WiimoteSource::None);
}

// Lie la manette n° 0 de config à l'appareil « Touchscreen » n° device_id du backend iOS de
// Dolphin, avec le profil Touchscreen.ini de Dolphin (Sys/Profiles/…). extension : réglage
// « Extension » du profil à remplacer (Wiimote), vide pour le garder.
static void BindTouchscreen(InputConfig* config, int device_id, const std::string& extension)
{
  if (!config || config->GetControllerCount() == 0)
    return;
  ciface::Core::DeviceQualifier touch;
  bool found = false;
  for (const auto& device : g_controller_interface.GetAllDevices())
  {
    if (device && device->GetSource() == "iOS" && device->GetName() == "Touchscreen" &&
        device->GetId() == device_id)
    {
      touch.FromDevice(device.get());
      found = true;
      break;
    }
  }
  if (!found)
  {
    NSLog(@"[GameCube] touchscreen device %d not found", device_id);
    return;
  }
  auto* pad = config->GetController(0);
  const std::string directory = config->GetSysProfileDirectoryPath();
  const std::string profile =
      directory + (directory.empty() || directory.back() == '/' ? "" : "/") + "Touchscreen.ini";
  Common::IniFile ini;
  if (File::Exists(profile) && ini.Load(profile))
  {
    auto* section = ini.GetOrCreateSection("Profile");
    if (!extension.empty())
      section->Set("Extension", extension);
    pad->LoadConfig(section);
  }
  else
  {
    pad->LoadDefaults(g_controller_interface);
  }
  pad->SetDefaultDevice(touch);
  pad->UpdateReferences(g_controller_interface);
  config->SaveConfig();
}

// Manette 1 de la GameCube = appareil n° 0 ; Wiimote 1 (émulée) = appareil n° 4. L'app les
// alimente elle-même (manette physique et contrôles tactiles) avec gc_set_input / gc_set_button /
// gc_set_axis. La manette GameCube reste branchée pour les jeux Wii qui l'acceptent.
static void BindControllers()
{
  BindTouchscreen(Pad::GetConfig(), 0, "");
  static const char* extensions[] = {"None", "Nunchuk", "Classic"};
  BindTouchscreen(Wiimote::GetConfig(), 4, extensions[std::clamp(s_wii_extension.load(), 0, 2)]);
}

static void StartInputPump()
{
  if (s_pump)
    return;
  s_pump = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0,
                                  dispatch_get_global_queue(QOS_CLASS_USER_INTERACTIVE, 0));
  dispatch_source_set_timer(s_pump, dispatch_time(DISPATCH_TIME_NOW, 0), NSEC_PER_MSEC * 8,
                            NSEC_PER_MSEC);
  dispatch_source_set_event_handler(s_pump, ^{
    Core::QueueHostJob(
        [](Core::System&) {
          g_controller_interface.SetCurrentInputChannel(ciface::InputChannel::Host);
          g_controller_interface.UpdateInput();
        },
        false);
  });
  dispatch_resume(s_pump);
}

static void StopInputPump()
{
  if (!s_pump)
    return;
  dispatch_source_cancel(s_pump);
  s_pump = nil;
}

bool gc_init(const char* user_dir)
{
  if (s_ready)
    return true;
  Core::DeclareAsHostThread();
  UICommon::SetUserDirectory(user_dir ? user_dir : "");
  UICommon::CreateDirectories();
  UICommon::Init();
  Common::RegisterMsgAlertHandler(&OnAlert);
  ApplySettings();

  WindowSystemInfo wsi;
  wsi.type = WindowSystemType::iOS;
  UICommon::InitControllers(wsi);

  State::SetOnAfterLoadCallback([] {
    s_load_ok = true;
    if (s_loaded)
      dispatch_semaphore_signal(s_loaded);
  });

  s_ready = true;
  NSLog(@"[GameCube] ready, user folder %s, sys folder %s", user_dir,
        File::GetSysDirectory().c_str());
  return true;
}

void* gc_metal_layer(void)
{
  if (!s_layer)
  {
    // alloc/init (et pas [GCMetalLayer layer]) : la couche est gardée même si ce fichier est compilé sans ARC
    // (sinon elle disparaissait en quittant un jeu, et la partie suivante plantait).
    s_layer = [[GCMetalLayer alloc] init];
    s_layer.device = MTLCreateSystemDefaultDevice();
    s_layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
    s_layer.opaque = YES;
  }
  return (__bridge void*)s_layer;
}

void gc_layout(double width, double height, double scale)
{
  if (!s_layer || width <= 0 || height <= 0)
    return;
  s_scale = (float)scale;
  s_layer.frame = CGRectMake(0, 0, width, height);
  s_layer.contentsScale = scale;
  const CGSize drawable = CGSizeMake(width * scale, height * scale);
  s_layer.drawableSize = drawable;
  if (!CGSizeEqualToSize(drawable, s_drawable))
  {
    s_drawable = drawable;
    if (g_presenter)
      g_presenter->ResizeSurface();
  }
}

bool gc_start(const char* path)
{
  if (!s_ready || !s_layer || !path || s_loop)
    return false;
  SetError("");
  s_loop = true;
  const std::string game = path;

  dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
    // Comme iCube : le fil principal cède le rôle d'« hôte » à la file d'attente de Dolphin.
    dispatch_sync(dispatch_get_main_queue(), ^{
      Core::UndeclareAsHostThread();
    });

    __block bool booted = false;
    DOLHostQueueRunSync(^{
      auto& system = Core::System::GetInstance();
      WindowSystemInfo wsi;
      wsi.type = WindowSystemType::iOS;
      wsi.render_surface = (__bridge void*)s_layer;
      wsi.render_surface_scale = s_scale;

      if (Config::GetLayer(Config::LayerType::CurrentRun))
        Config::DeleteKey(Config::LayerType::CurrentRun, Config::MAIN_CPU_CORE);
      Config::SetCurrent(Config::MAIN_CPU_CORE, PowerPC::CPUCore::CachedInterpreter);

      __block std::unique_ptr<BootParameters> boot =
          BootParameters::GenerateFromFile(std::vector<std::string>{game});
      if (!boot)
      {
        SetError("This file can't be read as a GameCube or Wii game.");
        return;
      }

      // Le rendu Metal modifie la couche : sur le fil principal (comme iCube).
      dispatch_sync(dispatch_get_main_queue(), ^{
        auto local_boot = std::move(boot);
        BindControllers();
        booted = BootManager::BootCore(system, std::move(local_boot), wsi);
      });
    });

    auto& system = Core::System::GetInstance();
    bool started = false;
    if (booted)
    {
      // Démarrage (compilation des shaders comprise). Resté « arrêté » 3 secondes : échec.
      int idle = 0;
      while (true)
      {
        const Core::State state = Core::GetState(system);
        if (state == Core::State::Running || state == Core::State::Paused)
        {
          started = true;
          break;
        }
        if (state == Core::State::Uninitialized && ++idle > 150)
          break;
        if (state != Core::State::Uninitialized)
          idle = 0;
        usleep(20 * 1000);
      }
    }
    if (started)
    {
      StartInputPump();
      while (Core::GetState(system) != Core::State::Uninitialized)
        usleep(100 * 1000);
      StopInputPump();
    }
    else
    {
      std::lock_guard lock(s_error_mutex);
      if (s_error.empty())
        s_error = "The game could not be started.";
    }

    dispatch_sync(dispatch_get_main_queue(), ^{
      Core::DeclareAsHostThread();
    });
    ciface::iOS::StateManager::GetInstance()->ClearController(0);
    ciface::iOS::StateManager::GetInstance()->ClearController(4);
    s_loop = false;
    NSLog(@"[GameCube] emulation ended");
  });
  return true;
}

int gc_state(void)
{
  if (!s_ready)
    return GC_STATE_STOPPED;
  switch (Core::GetState(Core::System::GetInstance()))
  {
  case Core::State::Starting:
    return GC_STATE_STARTING;
  case Core::State::Running:
    return GC_STATE_RUNNING;
  case Core::State::Paused:
    return GC_STATE_PAUSED;
  case Core::State::Stopping:
    return GC_STATE_STOPPING;
  case Core::State::Uninitialized:
  default:
    return s_loop ? GC_STATE_STARTING : GC_STATE_STOPPED;
  }
}

void gc_set_paused(bool paused)
{
  if (!s_loop)
    return;
  DOLHostQueueRunAsync(^{
    auto& system = Core::System::GetInstance();
    const Core::State state = Core::GetState(system);
    if (state == Core::State::Running || state == Core::State::Paused)
      Core::SetState(system, paused ? Core::State::Paused : Core::State::Running);
  });
}

void gc_stop(void)
{
  if (!s_loop)
    return;
  Host_Message(HostMessageID::WMUserStop);
  for (int i = 0; i < 200 && s_loop; ++i)  // 10 secondes au plus
    usleep(50 * 1000);
  if (s_loop)
    NSLog(@"[GameCube] stop timed out");
}

void gc_set_input(uint32_t buttons, float main_x, float main_y, float c_x, float c_y, float l,
                  float r)
{
  using ciface::iOS::ButtonType;
  auto* manager = ciface::iOS::StateManager::GetInstance();
  const std::pair<uint32_t, ButtonType> map[] = {
      {GC_BUTTON_A, ButtonType::BUTTON_A},         {GC_BUTTON_B, ButtonType::BUTTON_B},
      {GC_BUTTON_X, ButtonType::BUTTON_X},         {GC_BUTTON_Y, ButtonType::BUTTON_Y},
      {GC_BUTTON_Z, ButtonType::BUTTON_Z},         {GC_BUTTON_START, ButtonType::BUTTON_START},
      {GC_BUTTON_UP, ButtonType::BUTTON_UP},       {GC_BUTTON_DOWN, ButtonType::BUTTON_DOWN},
      {GC_BUTTON_LEFT, ButtonType::BUTTON_LEFT},   {GC_BUTTON_RIGHT, ButtonType::BUTTON_RIGHT},
  };
  for (const auto& [bit, button] : map)
    manager->SetButtonPressed(0, button, (buttons & bit) != 0);
  // Chaque direction lit la même valeur, signée (Touchscreen.mm inverse gauche et haut).
  manager->SetAxisValue(0, ButtonType::STICK_MAIN_LEFT, main_x);
  manager->SetAxisValue(0, ButtonType::STICK_MAIN_RIGHT, main_x);
  manager->SetAxisValue(0, ButtonType::STICK_MAIN_UP, main_y);
  manager->SetAxisValue(0, ButtonType::STICK_MAIN_DOWN, main_y);
  manager->SetAxisValue(0, ButtonType::STICK_C_LEFT, c_x);
  manager->SetAxisValue(0, ButtonType::STICK_C_RIGHT, c_x);
  manager->SetAxisValue(0, ButtonType::STICK_C_UP, c_y);
  manager->SetAxisValue(0, ButtonType::STICK_C_DOWN, c_y);
  manager->SetAxisValue(0, ButtonType::TRIGGER_L, l);
  manager->SetAxisValue(0, ButtonType::TRIGGER_R, r);
}

void gc_set_button(int device, int button, bool pressed)
{
  ciface::iOS::StateManager::GetInstance()->SetButtonPressed(
      device, static_cast<ciface::iOS::ButtonType>(button), pressed);
}

void gc_set_axis(int device, int axis, float value)
{
  ciface::iOS::StateManager::GetInstance()->SetAxisValue(
      device, static_cast<ciface::iOS::ButtonType>(axis), value);
}

int gc_disc_info(const char* path, char* game_id, int size)
{
  if (game_id && size > 0)
    game_id[0] = 0;
  if (!path)
    return GC_DISC_UNKNOWN;
  const std::unique_ptr<DiscIO::Volume> volume = DiscIO::CreateVolume(std::string(path));
  if (!volume)
    return GC_DISC_UNKNOWN;
  if (game_id && size > 0)
    strlcpy(game_id, volume->GetGameID().c_str(), (size_t)size);
  switch (volume->GetVolumeType())
  {
  case DiscIO::Platform::GameCubeDisc:
    return GC_DISC_GAMECUBE;
  case DiscIO::Platform::WiiDisc:
    return GC_DISC_WII;
  default:
    return GC_DISC_UNKNOWN;
  }
}

static double ModifiedTime(const std::string& path)
{
  struct stat info;
  if (stat(path.c_str(), &info) != 0)
    return -1;
  return info.st_mtimespec.tv_sec + info.st_mtimespec.tv_nsec / 1e9;
}

bool gc_save_state(const char* path)
{
  if (!s_loop || !path)
    return false;
  const std::string file = path;
  const double start = [NSDate date].timeIntervalSince1970 - 0.5;
  State::SaveAs(Core::System::GetInstance(), file);
  // L'écriture se termine sur un fil de Dolphin : le fichier apparaît (renommé) à la fin.
  for (int i = 0; i < 300; ++i)  // 15 secondes au plus
  {
    if (ModifiedTime(file) >= start)
      return true;
    usleep(50 * 1000);
  }
  return false;
}

bool gc_load_state(const char* path)
{
  if (!s_loop || !path || !File::Exists(path))
    return false;
  s_load_ok = false;
  s_loaded = dispatch_semaphore_create(0);
  State::LoadAs(Core::System::GetInstance(), path);
  const long timed_out =
      dispatch_semaphore_wait(s_loaded, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC));
  s_loaded = nil;
  return timed_out == 0 && s_load_ok;
}

bool gc_set_option(const char* name, double value)
{
  if (!name)
    return false;
  const std::string key = name;
  const bool on = value != 0;
  const int number = (int)value;
  if (key == "dual_core")
    Config::SetBase(Config::MAIN_CPU_THREAD, on);
  else if (key == "sync_gpu")
    Config::SetBase(Config::MAIN_SYNC_GPU, on);
  else if (key == "sync_on_skip_idle")
    Config::SetBase(Config::MAIN_SYNC_ON_SKIP_IDLE, on);
  else if (key == "dsp_thread")
    Config::SetBase(Config::MAIN_DSP_THREAD, on);
  else if (key == "fastmem")
    Config::SetBase(Config::MAIN_FASTMEM, on && FastmemAvailable());
  else if (key == "efb_access")
    Config::SetBase(Config::GFX_HACK_EFB_ACCESS_ENABLE, on);
  else if (key == "bbox")
    Config::SetBase(Config::GFX_HACK_BBOX_ENABLE, on);
  else if (key == "defer_efb_copies")
    Config::SetBase(Config::GFX_HACK_DEFER_EFB_COPIES, on);
  else if (key == "skip_efb_copy_to_ram")
    Config::SetBase(Config::GFX_HACK_SKIP_EFB_COPY_TO_RAM, on);
  else if (key == "skip_xfb_copy_to_ram")
    Config::SetBase(Config::GFX_HACK_SKIP_XFB_COPY_TO_RAM, on);
  else if (key == "immediate_xfb")
    Config::SetBase(Config::GFX_HACK_IMMEDIATE_XFB, on);
  else if (key == "efb_scale")
    Config::SetBase(Config::GFX_EFB_SCALE, std::clamp(number, 1, 4));
  else if (key == "shader_mode")
    Config::SetBase(Config::GFX_SHADER_COMPILATION_MODE,
                    static_cast<ShaderCompilationMode>(std::clamp(number, 0, 3)));
  else if (key == "vi_skip")
    Config::SetBase(Config::GFX_HACK_VI_SKIP_MODE, static_cast<TriState>(std::clamp(number, 0, 2)));
  else if (key == "wii_extension")
    s_wii_extension = std::clamp(number, 0, 2);
  else
    return false;
  NSLog(@"[GameCube] option %s = %g", name, value);
  return true;
}

void gc_set_cpu_clock(double factor)
{
  const float clock = std::clamp((float)factor, 0.3f, 1.0f);
  DOLHostQueueRunAsync(^{
    Config::SetCurrent(Config::MAIN_OVERCLOCK_ENABLE, clock < 0.999f);
    Config::SetCurrent(Config::MAIN_OVERCLOCK, clock);
  });
}

double gc_speed(void)
{
  if (!s_loop)
    return 0;
  return Core::System::GetInstance().GetPerfMetrics().GetSpeed();
}

double gc_fps(void)
{
  if (!s_loop)
    return 0;
  return Core::System::GetInstance().GetPerfMetrics().GetFPS();
}

void gc_last_error(char* buffer, int size)
{
  if (!buffer || size <= 0)
    return;
  std::lock_guard lock(s_error_mutex);
  strlcpy(buffer, s_error.c_str(), (size_t)size);
}
