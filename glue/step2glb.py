import subprocess
import os
import time
import argparse

def run_drawexe(input_filename, output_filename, mesh_quality=0.2, env_bat_path="env.bat", debug=False):
    # Проверка на допустимость значения качества тесселяции
    if not (0.01 <= mesh_quality <= 1):
        raise ValueError("mesh_quality должен быть в пределах от 0.01 до 1.")
    
    # TCL использует / как разделитель путей; абсолютные пути обязательны
    def _tcl(p):
        return os.path.abspath(p).replace("\\", "/")

    tcl_script = f"""
pload XDE OCAF MODELING
ReadStep D "{_tcl(input_filename)}"
XGetOneShape s D
incmesh s {mesh_quality}
WriteGltf D "{_tcl(output_filename)}"
"""
    
    # env.bat использует %CD%, поэтому все temp-файлы пишем в директорию env.bat
    env_bat_dir = os.path.dirname(os.path.abspath(env_bat_path))

    # Запись TCL-скрипта во временный файл tmpscript.tcl
    tcl_script_filename = os.path.join(env_bat_dir, "tmpscript.tcl")
    with open(tcl_script_filename, 'w') as f:
        f.write(tcl_script)
    
    # Если включен режим отладки, выводим содержимое TCL-скрипта
    if debug:
        print("\nСодержимое временного TCL скрипта (tmpscript.tcl):")
        with open(tcl_script_filename, 'r') as f:
            print(f.read())

    # Формируем BAT-скрипт
    bat_script = f"""
@echo off
call "{env_bat_path}"
DRAWEXE.exe -f tmpscript.tcl
"""
    
    # Запись BAT-скрипта во временный файл
    bat_script_filename = os.path.join(env_bat_dir, "temp_script.bat")
    with open(bat_script_filename, 'w') as f:
        f.write(bat_script)

    # Если включен режим отладки, выводим содержимое BAT-скрипта
    if debug:
        print("\nСодержимое временного BAT скрипта (temp_script.bat):")
        with open(bat_script_filename, 'r') as f:
            print(f.read())

    # Запуск .bat файла для настройки окружения и вызова DRAWEXE.exe
    try:
        # Выполним BAT-скрипт через subprocess из директории env.bat
        subprocess.run([bat_script_filename], check=True, shell=True,
                       cwd=env_bat_dir)
        
        # Пауза, чтобы дождаться завершения процесса и появления выходного файла
        time.sleep(2)  # Увеличьте время ожидания, если необходимо
        
        # Проверяем, появился ли выходной файл
        if os.path.exists(output_filename):
            print(f"Конвертация завершена успешно. Выходной файл: {output_filename}")
        else:
            print(f"Ошибка: выходной файл не найден. Конвертация не удалась.")
    
    except subprocess.CalledProcessError as e:
        print(f"Ошибка при выполнении BAT-скрипта: {e}")
    
    finally:
        # Удаляем временные файлы (TCL и BAT скрипты)
        if os.path.exists(tcl_script_filename):
            os.remove(tcl_script_filename)
        if os.path.exists(bat_script_filename):
            os.remove(bat_script_filename)

# Парсинг аргументов командной строки
def parse_args():
    parser = argparse.ArgumentParser(description="Конвертация STEP файла в GLTF с использованием DRAWEXE.")
    parser.add_argument("input_file", help="Путь к входному STEP файлу")
    parser.add_argument("output_file", help="Путь к выходному GLTF файлу")
    parser.add_argument("-q", "--quality", type=float, default=0.2, help="Качество тесселяции (от 0.01 до 1.0, по умолчанию 0.2)")
    parser.add_argument("-e", "--env_bat", default="env.bat", help="Путь к файлу env.bat (по умолчанию 'env.bat')")
    parser.add_argument("-d", "--debug", action="store_true", help="Включить вывод содержимого временных скриптов для отладки")
    return parser.parse_args()

def main():
    # Получаем аргументы командной строки
    args = parse_args()

    # Запускаем процесс конвертации с флагом debug
    run_drawexe(args.input_file, args.output_file, args.quality, args.env_bat, args.debug)

if __name__ == "__main__":
    main()