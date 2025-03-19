# Jak II /3 - All Missions Speedrun Mod (OpenGOAL)

- Play now via the JakMods mod list! https://jakmods.dev

- Adds Individual Level categories to the speedrunner menu (hold `L1`+`R1`+`X` and press `Start` or `Select`)
<img src="https://github.com/user-attachments/assets/2719a96a-2eae-4912-8773-28beea359423" width="800"/>

- Adds in-game timer and end screen after completing each mission, with some statistics (more to come)

![image](https://github.com/user-attachments/assets/00ac1184-8ca4-4e15-9309-4e3eaf6d8550)

- For Jak 3 you can install via these steps (Windows only):
  - Install `git` and `task` (see https://github.com/open-goal/jak-project?tab=readme-ov-file#required-software)
  - Run this in powershell/terminal from the folder where you want to install:
    - `git clone https://github.com/OpenGOAL-Mods/OG-ILs.git`
  - Copy the contents of your Jak 3 ISO to `OG-ILs/iso_data/jak3`
  - Back in powershell/terminal run these commands:
    - `cd OG-ILs`
    - `task set-game-jak3`
    - `task extract`
    - `task repl`
      - `(mi)`
      - `(e)` (after the `(mi)` completes 100%)
    - `task boot-game-retail`
