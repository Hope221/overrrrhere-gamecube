// Pont GameCube et Wii (cœur Dolphin d'iCube) pour l'app overrrrhere : une API C simple,
// sans en-tête Dolphin, appelée depuis Swift.
// SPDX-License-Identifier: GPL-2.0-or-later

#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GC_API __attribute__((visibility("default")))

// Boutons de gc_set_input (bits).
enum {
  GC_BUTTON_A = 1 << 0,
  GC_BUTTON_B = 1 << 1,
  GC_BUTTON_X = 1 << 2,
  GC_BUTTON_Y = 1 << 3,
  GC_BUTTON_Z = 1 << 4,
  GC_BUTTON_START = 1 << 5,
  GC_BUTTON_UP = 1 << 6,
  GC_BUTTON_DOWN = 1 << 7,
  GC_BUTTON_LEFT = 1 << 8,
  GC_BUTTON_RIGHT = 1 << 9,
};

// États de gc_state.
enum {
  GC_STATE_STOPPED = 0,
  GC_STATE_STARTING = 1,
  GC_STATE_RUNNING = 2,
  GC_STATE_PAUSED = 3,
  GC_STATE_STOPPING = 4,
};

// Système d'un disque (gc_disc_info).
enum {
  GC_DISC_UNKNOWN = -1,
  GC_DISC_GAMECUBE = 0,
  GC_DISC_WII = 1,
};

// Appareils de gc_set_button / gc_set_axis : manette GameCube 1 et Wiimote 1.
enum {
  GC_DEVICE_GAMECUBE = 0,
  GC_DEVICE_WIIMOTE = 4,
};

// Une seule fois, sur le fil principal : dossier de travail de Dolphin (cartes mémoire,
// cache des shaders, réglages). Le dossier « Sys » de Dolphin doit être à la racine de l'app.
GC_API bool gc_init(const char* user_dir);

// Couche d'affichage Metal à placer dans une vue (CAMetalLayer*, gardée par le pont).
// Sur le fil principal.
GC_API void* gc_metal_layer(void);

// Taille de la vue en points et échelle de l'écran : à appeler à chaque changement de taille.
GC_API void gc_layout(double width, double height, double scale);

// Lance le jeu GameCube ou Wii (chemin du fichier .iso, .gcm, .rvz, .wbfs…). Retour immédiat ; suivre gc_state.
// Sur le fil principal.
GC_API bool gc_start(const char* path);

GC_API int gc_state(void);
GC_API void gc_set_paused(bool paused);

// Arrête le jeu et attend la fin (quelques secondes au plus). Pas sur le fil principal.
GC_API void gc_stop(void);

// Manette : boutons GC_BUTTON_*, sticks de -1 à 1 (bas = +1), gâchettes de 0 à 1.
GC_API void gc_set_input(uint32_t buttons, float main_x, float main_y, float c_x, float c_y,
                         float l, float r);

// Bouton ou axe d'un appareil GC_DEVICE_*, par son numéro dans ButtonType.h du backend iOS de
// Dolphin (Wiimote 100…, Nunchuk 200…, Classic Controller 300…). Axe : de -1 à 1 pour un stick
// (chaque direction lit la même valeur signée), de 0 à 1 pour une gâchette.
GC_API void gc_set_button(int device, int button, bool pressed);
GC_API void gc_set_axis(int device, int axis, float value);

// Change la manette branchée sur la Wiimote pendant la partie (0 aucune, 1 Nunchuk, 2 Classic Controller).
GC_API void gc_set_wii_extension(int extension);

// Système du disque (GC_DISC_*) et identifiant du jeu (ex. « RMCE01 »), copié dans game_id.
// N'importe quel fil, sans gc_init.
GC_API int gc_disc_info(const char* path, char* game_id, int size);

// Sauvegarde / charge une partie (fichier d'état). Attendent la fin. Pas sur le fil principal.
GC_API bool gc_save_state(const char* path);
GC_API bool gc_load_state(const char* path);

// Réglage de Dolphin, avant gc_start (valeur numérique ; 0 / 1 pour oui / non). Faux si le nom est inconnu.
// dual_core, sync_gpu, sync_on_skip_idle, dsp_thread, fastmem, efb_access, bbox, defer_efb_copies,
// skip_efb_copy_to_ram, skip_xfb_copy_to_ram, immediate_xfb, efb_scale, shader_mode (0 spécialisés,
// 1 ubershaders, 2 hybride, 3 sans attendre), vi_skip (0 non, 1 oui, 2 auto),
// wii_extension (manette branchée sur la Wiimote : 0 aucune, 1 Nunchuk, 2 Classic Controller).
GC_API bool gc_set_option(const char* name, double value);

// Vitesse du processeur simulé pendant la partie : 1 = normale, jusqu'à 0,3 (moins de calcul, le jeu
// peut ralentir un peu dans les passages chargés). Comme l'« horloge adaptative » d'iCube.
GC_API void gc_set_cpu_clock(double factor);

// Image sur tout l'écran (Fill screen) : « widescreen hack » de Dolphin + image étirée à la vue. La scène 3D est
// élargie à la forme de l'écran (plus de décor sur les côtés) au lieu d'être déformée. Avant ou pendant la partie.
GC_API void gc_set_fill_screen(bool fill);

// Image du jeu en PNG (vignettes des sauvegardes d'état). Dolphin l'écrit à la prochaine image affichée : le jeu
// doit tourner (pas en pause). Attend la fin, 1,5 seconde au plus ; faux sinon. Pas sur le fil principal.
GC_API bool gc_capture_frame(const char* path);

// Vitesse de l'émulation (1 = vitesse de la console) et images par seconde. Mesure temporaire.
GC_API double gc_speed(void);
GC_API double gc_fps(void);

// Dernier message d'erreur de Dolphin (vide si aucun). Copié dans buffer.
GC_API void gc_last_error(char* buffer, int size);

#ifdef __cplusplus
}
#endif
