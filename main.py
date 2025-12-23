#!/usr/bin/env python3
"""
Daily Portrait Timelapse Application
A cross-platform app for taking daily portraits and creating timelapses
"""

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib
import cv2
import os
import json
from datetime import datetime
from pathlib import Path
import subprocess
import threading
import platform
import argparse
import sys
import queue
import time

# Debug mode flag
DEBUG = False

def debug_print(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)

SYSTEM = platform.system()
debug_print(f"Detected OS: {SYSTEM}")

# Set config file location based on OS
if SYSTEM == 'Windows':
    CONFIG_FILE = str(Path.home() / "AppData" / "Local" / "DailyPortrait" / "config.json")
elif SYSTEM == 'Darwin': 
    CONFIG_FILE = str(Path.home() / "Library" / "Application Support" / "DailyPortrait" / "config.json")
else:  # Linux 
    CONFIG_FILE = str(Path.home() / ".config" / "daily_portrait" / "config.json")

os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
debug_print(f"Config file location: {CONFIG_FILE}")

class Config:
    def __init__(self):
        if SYSTEM == 'Windows':
            default_photos = str(Path.home() / "Pictures" / "DailyPortraits")
        elif SYSTEM == 'Darwin':
            default_photos = str(Path.home() / "Pictures" / "DailyPortraits")
        else:
            xdg_pictures = os.environ.get('XDG_PICTURES_DIR')
            if xdg_pictures:
                default_photos = str(Path(xdg_pictures) / "DailyPortraits")
            else:
                default_photos = str(Path.home() / "Pictures" / "DailyPortraits")
        
        self.data = {
            'photos_directory': default_photos,
            'guide_enabled': True,
            'guide_x': 0.5,
            'guide_y': 0.5,
            'guide_width': 0.3,
            'guide_height': 0.4
        }
        self.load()
        self._save_pending = False
        self._save_timer = None
    
    def load(self):
        debug_print(f"Loading config from {CONFIG_FILE}")
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
                debug_print(f"Config loaded successfully")
        except Exception as e:
            debug_print(f"Error loading config: {e}")
            print(f"Error loading config: {e}")
    
    def _do_save(self):
        """Actually perform the save operation"""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.data, f, indent=2)
            self._save_pending = False
            self._save_timer = None
            return False  # Don't repeat
        except Exception as e:
            print(f"Error saving config: {e}")
            return False
    
    def save(self):
        """Defer save to avoid blocking on every config change"""
        if not self._save_pending:
            self._save_pending = True
            # Cancel existing timer if any
            if self._save_timer:
                GLib.source_remove(self._save_timer)
            # Save after 500ms of no changes
            self._save_timer = GLib.timeout_add(500, self._do_save)
    
    def get(self, key):
        return self.data.get(key)
    
    def set(self, key, value):
        self.data[key] = value
        self.save()

class CameraThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.camera = None
        self.running = False
        self.frame_queue = queue.Queue(maxsize=2)  # Limit queue size
        self.lock = threading.Lock()
        
    def run(self):
        debug_print("Camera thread starting...")
        try:
            self.camera = cv2.VideoCapture(0)
            if not self.camera.isOpened():
                debug_print("ERROR: Failed to open camera!")
                return
            
            debug_print("Camera opened successfully in thread")
            self.running = True
            
            while self.running:
                ret, frame = self.camera.read()
                if ret:
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    
                    try:
                        self.frame_queue.put_nowait(frame_rgb)
                    except queue.Full:
                        pass  # Drop frame if queue is full
                else:
                    time.sleep(0.01) 
                    
        except Exception as e:
            debug_print(f"Camera thread error: {e}")
        finally:
            if self.camera:
                self.camera.release()
            debug_print("Camera thread stopped")
    
    def get_frame(self):
        """Get the latest frame (non-blocking)"""
        try:
            # Get most recent frame, discard older ones
            frame = None
            while not self.frame_queue.empty():
                frame = self.frame_queue.get_nowait()
            return frame
        except queue.Empty:
            return None
    
    def stop(self):
        self.running = False

class CameraView(Gtk.Box):
    def __init__(self, config, on_photo_taken):
        debug_print("Initializing CameraView")
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.config = config
        self.on_photo_taken_callback = on_photo_taken
        self.camera_thread = None
        self.captured_frame = None
        self.preview_mode = False
        self.current_frame = None
        self.cached_pixbuf = None
        self.last_frame_size = None
        
        debug_print("Creating drawing area for camera feed")
        
        # Create drawing area for camera feed
        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_size_request(640, 480)
        self.drawing_area.connect('draw', self.on_draw)
        self.pack_start(self.drawing_area, True, True, 0)
        
        # Guide controls
        guide_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        guide_box.set_halign(Gtk.Align.CENTER)
        
        self.guide_toggle = Gtk.CheckButton(label="Show Face Guide")
        self.guide_toggle.set_active(config.get('guide_enabled'))
        self.guide_toggle.connect('toggled', self.on_guide_toggled)
        guide_box.pack_start(self.guide_toggle, False, False, 0)
        
        guide_label = Gtk.Label(label="Drag guide to position")
        guide_box.pack_start(guide_label, False, False, 0)
        
        self.pack_start(guide_box, False, False, 0)
        
        # Buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        button_box.set_halign(Gtk.Align.CENTER)
        
        self.capture_button = Gtk.Button(label="Take Photo")
        self.capture_button.connect('clicked', self.on_capture)
        button_box.pack_start(self.capture_button, False, False, 0)
        
        self.save_button = Gtk.Button(label="Save Photo")
        self.save_button.connect('clicked', self.on_save)
        self.save_button.set_sensitive(False)
        button_box.pack_start(self.save_button, False, False, 0)
        
        self.discard_button = Gtk.Button(label="Discard & Retry")
        self.discard_button.connect('clicked', self.on_discard)
        self.discard_button.set_sensitive(False)
        button_box.pack_start(self.discard_button, False, False, 0)
        
        self.pack_start(button_box, False, False, 0)
        
        # Mouse events for dragging guide
        self.drawing_area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                                     Gdk.EventMask.BUTTON_RELEASE_MASK |
                                     Gdk.EventMask.POINTER_MOTION_MASK)
        self.drawing_area.connect('button-press-event', self.on_button_press)
        self.drawing_area.connect('motion-notify-event', self.on_motion)
        self.drawing_area.connect('button-release-event', self.on_button_release)
        
        self.dragging = False
        
        debug_print("CameraView initialization complete")
    
    def start_camera(self):
        if not self.camera_thread:
            debug_print("Starting camera thread...")
            self.camera_thread = CameraThread()
            self.camera_thread.start()
            # Slower update rate - 60fps is overkill for preview
            GLib.timeout_add(50, self.update_frame)  # 20fps is plenty
    
    def stop_camera(self):
        if self.camera_thread:
            debug_print("Stopping camera thread...")
            self.camera_thread.stop()
            self.camera_thread = None
    
    def update_frame(self):
        if self.camera_thread and self.camera_thread.running and not self.preview_mode:
            frame = self.camera_thread.get_frame()
            if frame is not None:
                self.current_frame = frame
                self.drawing_area.queue_draw()
            return True
        return self.camera_thread and self.camera_thread.running
    
    def on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        width, height = alloc.width, alloc.height
        
        # Draw camera frame or captured photo
        frame_to_draw = self.captured_frame if self.preview_mode else self.current_frame
        
        if frame_to_draw is not None:
            h, w = frame_to_draw.shape[:2]
            
            # Scale to fit
            scale = min(width / w, height / h)
            new_w, new_h = int(w * scale), int(h * scale)
            
            # Center the image
            x_offset = (width - new_w) // 2
            y_offset = (height - new_h) // 2
            
            # Cache pixbuf if size hasn't changed (optimization)
            frame_size = (w, h, new_w, new_h)
            if self.last_frame_size != frame_size or self.cached_pixbuf is None:
                pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                    frame_to_draw.tobytes(),
                    GdkPixbuf.Colorspace.RGB,
                    False, 8, w, h, w * 3
                )
                self.cached_pixbuf = pixbuf.scale_simple(
                    new_w, new_h, GdkPixbuf.InterpType.BILINEAR
                )
                self.last_frame_size = frame_size
            
            Gdk.cairo_set_source_pixbuf(cr, self.cached_pixbuf, x_offset, y_offset)
            cr.paint()
            
            # Draw face guide overlay (only in live view, not in preview)
            if self.config.get('guide_enabled') and not self.preview_mode:
                guide_x = self.config.get('guide_x') * width
                guide_y = self.config.get('guide_y') * height
                guide_w = self.config.get('guide_width') * width
                guide_h = self.config.get('guide_height') * height
                
                cr.save()
                cr.translate(guide_x, guide_y)
                cr.scale(guide_w / 2, guide_h / 2)
                cr.arc(0, 0, 1, 0, 2 * 3.14159)
                cr.restore()
                
                cr.set_source_rgba(1, 1, 1, 0.5)
                cr.set_line_width(2)
                cr.stroke()
    
    def on_guide_toggled(self, button):
        self.config.set('guide_enabled', button.get_active())
        self.drawing_area.queue_draw()
    
    def on_button_press(self, widget, event):
        if self.config.get('guide_enabled') and not self.preview_mode:
            alloc = widget.get_allocation()
            guide_x = self.config.get('guide_x') * alloc.width
            guide_y = self.config.get('guide_y') * alloc.height
            guide_w = self.config.get('guide_width') * alloc.width
            guide_h = self.config.get('guide_height') * alloc.height
            
            # Check if click is near guide
            dx = (event.x - guide_x) / (guide_w / 2)
            dy = (event.y - guide_y) / (guide_h / 2)
            if dx*dx + dy*dy <= 1:
                self.dragging = True
    
    def on_motion(self, widget, event):
        if self.dragging:
            alloc = widget.get_allocation()
            self.config.set('guide_x', event.x / alloc.width)
            self.config.set('guide_y', event.y / alloc.height)
            self.drawing_area.queue_draw()
    
    def on_button_release(self, widget, event):
        self.dragging = False
    
    def on_capture(self, button):
        if self.current_frame is not None:
            self.captured_frame = self.current_frame.copy()
            self.preview_mode = True
            self.capture_button.set_sensitive(False)
            self.save_button.set_sensitive(True)
            self.discard_button.set_sensitive(True)
            self.guide_toggle.set_sensitive(False)
            self.cached_pixbuf = None  # Clear cache for preview
            self.drawing_area.queue_draw()
    
    def on_save(self, button):
        if self.captured_frame is not None:
            # Convert back to BGR for saving
            frame_bgr = cv2.cvtColor(self.captured_frame, cv2.COLOR_RGB2BGR)
            self.on_photo_taken_callback(frame_bgr)
            self.reset_capture()
    
    def on_discard(self, button):
        self.reset_capture()
    
    def reset_capture(self):
        self.captured_frame = None
        self.preview_mode = False
        self.capture_button.set_sensitive(True)
        self.save_button.set_sensitive(False)
        self.discard_button.set_sensitive(False)
        self.guide_toggle.set_sensitive(True)
        self.cached_pixbuf = None  # Clear cache
        self.drawing_area.queue_draw()

class TimelapseView(Gtk.Box):
    """Timelapse creation view"""
    def __init__(self, config):
        debug_print("Initializing TimelapseView")
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.config = config
        self.set_margin_start(20)
        self.set_margin_end(20)
        self.set_margin_top(20)
        self.set_margin_bottom(20)
        
        # Title
        title = Gtk.Label()
        title.set_markup("<big><b>Create Timelapse</b></big>")
        self.pack_start(title, False, False, 0)
        
        # Photo count
        self.count_label = Gtk.Label(label="Counting photos...")
        self.pack_start(self.count_label, False, False, 0)
        
        # Duration slider
        slider_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        
        slider_label = Gtk.Label(label="Duration per photo (seconds):")
        slider_label.set_halign(Gtk.Align.START)
        slider_box.pack_start(slider_label, False, False, 0)
        
        self.duration_adj = Gtk.Adjustment(value=0.2, lower=0.05, upper=0.5, 
                                           step_increment=0.05, page_increment=0.1)
        self.duration_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                       adjustment=self.duration_adj)
        self.duration_scale.set_digits(2)
        self.duration_scale.set_value_pos(Gtk.PositionType.RIGHT)
        slider_box.pack_start(self.duration_scale, False, False, 0)
        
        self.pack_start(slider_box, False, False, 0)
        
        # Create button
        self.create_button = Gtk.Button(label="Create Timelapse")
        self.create_button.connect('clicked', self.on_create)
        self.pack_start(self.create_button, False, False, 0)
        
        # Status label
        self.status_label = Gtk.Label(label="")
        self.pack_start(self.status_label, False, False, 0)
        
        # Progress bar
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.pack_start(self.progress, False, False, 0)
        
        # Defer photo counting to avoid blocking UI on startup
        GLib.idle_add(self.update_photo_count)
    
    def update_photo_count(self):
        photos_dir = self.config.get('photos_directory')
        if os.path.exists(photos_dir):
            photos = [f for f in os.listdir(photos_dir) 
                     if f.endswith(('.jpg', '.png'))]
            count = len(photos)
            self.count_label.set_text(f"Found {count} photo(s) in directory")
            self.create_button.set_sensitive(count > 0)
        else:
            self.count_label.set_text("No photos directory found")
            self.create_button.set_sensitive(False)
        return False  # Don't repeat
    
    def on_create(self, button):
        photos_dir = self.config.get('photos_directory')
        duration = self.duration_adj.get_value()
        
        # Get all photos sorted by filename (date)
        photos = sorted([f for f in os.listdir(photos_dir) 
                        if f.endswith(('.jpg', '.png'))])
        
        if len(photos) == 0:
            self.status_label.set_text("No photos found!")
            return
        
        button.set_sensitive(False)
        self.status_label.set_text("Creating timelapse...")
        self.progress.set_fraction(0)
        
        # Run ffmpeg in thread
        thread = threading.Thread(target=self.create_timelapse_thread,
                                 args=(photos_dir, photos, duration))
        thread.daemon = True
        thread.start()
    
    def create_timelapse_thread(self, photos_dir, photos, duration):
        try:
            # Calculate framerate
            fps = 1.0 / duration
            
            # Create output filename
            output_file = os.path.join(photos_dir, 
                                      f"timelapse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
            
            # Create temporary file list for ffmpeg
            list_file = os.path.join(photos_dir, 'filelist.txt')
            with open(list_file, 'w') as f:
                for photo in photos:
                    # Escape single quotes in filenames for concat demuxer
                    escaped = photo.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
                    f.write(f"duration {duration}\n")
                # Add last image again (ffmpeg concat quirk)
                if photos:
                    escaped = photos[-1].replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
            
            # Run ffmpeg
            ffmpeg_cmd = 'ffmpeg.exe' if SYSTEM == 'Windows' else 'ffmpeg'
            
            cmd = [
                ffmpeg_cmd, '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', list_file,
                '-vsync', 'vfr',
                '-pix_fmt', 'yuv420p',
                '-c:v', 'libx264',  # Explicitly specify codec
                '-preset', 'medium',  # Balance quality/speed
                output_file
            ]
            
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, 
                                      stderr=subprocess.PIPE,
                                      cwd=photos_dir)  # Set working directory
            stdout, stderr = process.communicate()
            
            # Clean up
            try:
                os.remove(list_file)
            except:
                pass
            
            if process.returncode == 0:
                GLib.idle_add(self.on_timelapse_complete, output_file)
            else:
                GLib.idle_add(self.on_timelapse_error, stderr.decode())
        
        except Exception as e:
            GLib.idle_add(self.on_timelapse_error, str(e))
    
    def on_timelapse_complete(self, output_file):
        self.status_label.set_text(f"Timelapse created: {os.path.basename(output_file)}")
        self.progress.set_fraction(1.0)
        self.create_button.set_sensitive(True)
        return False
    
    def on_timelapse_error(self, error_msg):
        self.status_label.set_text(f"Error: {error_msg[:100]}")
        self.progress.set_fraction(0)
        self.create_button.set_sensitive(True)
        return False

class MainWindow(Gtk.Window):
    """Main application window"""
    def __init__(self):
        debug_print("Initializing MainWindow")
        super().__init__(title="Daily Portrait Timelapse")
        self.set_default_size(800, 600)
        self.connect('destroy', self.on_destroy)
        
        debug_print("Loading configuration")
        self.config = Config()
        
        # Create photos directory if it doesn't exist
        photos_dir = self.config.get('photos_directory')
        debug_print(f"Photos directory: {photos_dir}")
        os.makedirs(photos_dir, exist_ok=True)
        
        debug_print("Creating main UI")
        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(main_box)
        
        debug_print("Creating notebook tabs")
        # Create notebook for tabs
        self.notebook = Gtk.Notebook()
        main_box.pack_start(self.notebook, True, True, 0)
        
        # Camera tab
        debug_print("Creating camera view")
        self.camera_view = CameraView(self.config, self.on_photo_taken)
        self.notebook.append_page(self.camera_view, Gtk.Label(label="Take Photo"))
        
        # Timelapse tab
        debug_print("Creating timelapse view")
        self.timelapse_view = TimelapseView(self.config)
        self.notebook.append_page(self.timelapse_view, Gtk.Label(label="Create Timelapse"))
        
        debug_print("Creating settings button")
        # Settings button
        settings_button = Gtk.Button(label="Choose Photos Directory")
        settings_button.connect('clicked', self.on_choose_directory)
        main_box.pack_start(settings_button, False, False, 5)
        
        # Check if photo taken today (deferred to avoid blocking)
        GLib.idle_add(self.check_today_photo)
        
        debug_print("MainWindow initialization complete")
    
    def check_today_photo(self):
        today = datetime.now().strftime('%Y-%m-%d')
        photos_dir = self.config.get('photos_directory')
        
        if os.path.exists(photos_dir):
            existing = [f for f in os.listdir(photos_dir) if f.startswith(today)]
            if existing:
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    flags=0,
                    message_type=Gtk.MessageType.INFO,
                    buttons=Gtk.ButtonsType.OK,
                    text="Photo Already Taken Today"
                )
                dialog.format_secondary_text(
                    f"You've already taken a photo today: {existing[0]}"
                )
                dialog.run()
                dialog.destroy()
        return False  # Don't repeat
    
    def on_photo_taken(self, frame):
        """Handle photo taken - run in thread to avoid UI blocking"""
        def save_photo():
            today = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            photos_dir = self.config.get('photos_directory')
            filename = os.path.join(photos_dir, f"{today}.jpg")
            
            cv2.imwrite(filename, frame)
            
            GLib.idle_add(self.show_photo_saved_dialog, filename)
        
        thread = threading.Thread(target=save_photo)
        thread.daemon = True
        thread.start()
    
    def show_photo_saved_dialog(self, filename):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Photo Saved!"
        )
        dialog.format_secondary_text(f"Saved to: {filename}")
        dialog.run()
        dialog.destroy()
        
        # Update timelapse view
        self.timelapse_view.update_photo_count()
        return False
    
    def on_choose_directory(self, button):
        dialog = Gtk.FileChooserDialog(
            title="Choose Photos Directory",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        
        dialog.set_current_folder(self.config.get('photos_directory'))
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            new_dir = dialog.get_filename()
            self.config.set('photos_directory', new_dir)
            os.makedirs(new_dir, exist_ok=True)
            self.timelapse_view.update_photo_count()
        
        dialog.destroy()
    
    def on_destroy(self, widget):
        debug_print("Window closing, stopping camera")
        self.camera_view.stop_camera()
        debug_print("Exiting GTK main loop")
        Gtk.main_quit()

def main():
    parser = argparse.ArgumentParser(description='Daily Portrait Timelapse Application')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    args = parser.parse_args()
    
    global DEBUG
    DEBUG = args.debug
    
    debug_print("=== Daily Portrait Timelapse Starting ===")
    debug_print(f"Python version: {sys.version}")
    debug_print(f"OpenCV version: {cv2.__version__}")
    debug_print(f"GTK version: {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
    
    # Check for ffmpeg
    debug_print("Checking for ffmpeg...")
    try:
        ffmpeg_cmd = 'ffmpeg.exe' if SYSTEM == 'Windows' else 'ffmpeg'
        result = subprocess.run([ffmpeg_cmd, '-version'], 
                      stdout=subprocess.PIPE, 
                      stderr=subprocess.PIPE,
                      timeout=5)
        debug_print(f"ffmpeg found: {result.returncode == 0}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        debug_print("ffmpeg NOT found")
        print("WARNING: ffmpeg not found. Timelapse feature will not work.")
        if SYSTEM == 'Windows':
            print("  Windows: Download from https://ffmpeg.org/download.html")
        elif SYSTEM == 'Darwin':
            print("  macOS: brew install ffmpeg")
        else:
            print("  Linux: sudo apt install ffmpeg (Debian/Ubuntu)")
    
    # Camera backend optimization for Windows
    if SYSTEM == 'Windows':
        os.environ['OPENCV_VIDEOIO_PRIORITY_MSMF'] = '0'
        debug_print("Set Windows camera backend preference")
    
    debug_print("Creating main window...")
    win = MainWindow()
    debug_print("Showing window...")
    win.show_all()
    
    # Start camera after GUI is shown
    def init_camera():
        debug_print("Initializing camera...")
        win.camera_view.start_camera()
        return False
    
    GLib.idle_add(init_camera)
    
    debug_print("Entering GTK main loop...")
    Gtk.main()
    debug_print("Application closed cleanly")

if __name__ == '__main__':
    main()
