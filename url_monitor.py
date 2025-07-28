import wx
import wx.adv
import wx.html2
import wx.grid
import requests # Keep requests for fallbacks or initial checks if needed, though we'll pivot to webview
import time
import threading
import os
import pickle
import re
from bs4 import BeautifulSoup
import json
import wx.lib.newevent


# --- Configuration ---
APP_NAME = "URL Change Monitor (WebView)"
DATA_FILE = "url_monitor_data.pkl"

# --- Custom Events for inter-thread communication ---
RequestWebViewLoadEvent, EVT_REQUEST_WEBVIEW_LOAD = wx.lib.newevent.NewEvent()
WebViewLoadCompletedEvent, EVT_WEBVIEW_LOAD_COMPLETED = wx.lib.newevent.NewEvent()
WebViewLoadFailedEvent, EVT_WEBVIEW_LOAD_FAILED = wx.lib.newevent.NewEvent()



class URLMonitor:
    def __init__(self, url, interval=300, enabled=True, tag="", selector_type="", selector_value=""):
        self.url = url
        self.interval = interval  # in seconds
        self.enabled = enabled
        self.last_check_time = 0
        self.last_source = ""     # Will store the content of the monitored element
        self.tag = tag            # e.g., 'div', 'span', 'p'
        self.selector_type = selector_type # 'id' or 'class'
        self.selector_value = selector_value # The actual id or class name
        self.last_change_time = None 
        self.ignored_count = 0 # Count of times a check was skipped due to interval
        self.check_count = 0 # Total times check_for_changes was called (regardless of interval)
        self.monitored_check_count = 0 # Total times an actual load request was made

    def should_check(self):
        """Checks if it's time to schedule a check based on the interval."""
        current_timestamp = time.time()
        if self.enabled and current_timestamp - self.last_check_time >= self.interval:
            return True
        else:
            self.ignored_count += 1
            return False

    # This method still exists but primarily manages the check timing.
    def schedule_check(self, app_frame):
         """Schedules a check by sending an event to the UI thread."""
         if self.should_check():
             print(f"Scheduling webview check for {self.url}")
             self.check_count += 1
             event = RequestWebViewLoadEvent(url=self.url)
             wx.PostEvent(app_frame, event) # Post event to the frame (UI thread)


class AppFrame(wx.Frame):
    def __init__(self, parent, title):
        super(AppFrame, self).__init__(parent, title=title, size=(1200, 700)) # Increased size

        self.urls_to_monitor = {}  # Dictionary to store URLMonitor objects {url: URLMonitor}
        self.monitoring_thread = None
        self.monitoring_running = False
        self.webview_panel = None
        self.webview = None
        self.webview_loading_url = None # Track the URL currently being loaded in WebView
        self.check_queue = [] # Use a list as a simple queue for URLs to check

        self.create_ui()
        self.load_data()
        self.update_list_ctrl()

        self.Centre()
        self.Show()

       
        self.Bind(EVT_REQUEST_WEBVIEW_LOAD, self.on_request_webview_load)
        self.Bind(EVT_WEBVIEW_LOAD_COMPLETED, self.on_webview_load_completed)
        self.Bind(EVT_WEBVIEW_LOAD_FAILED, self.on_webview_load_failed)

        
        self.Bind(wx.EVT_CLOSE, self.on_close)


    def create_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL) # Main sizer stacks content vertically
        
        splitter = wx.SplitterWindow(panel, style=wx.SP_3D | wx.SP_LIVE_UPDATE) # Added style for visual cues
        splitter.SetMinimumPaneSize(100) # Minimum size for either pane
        
        # --- Left Panel (Inputs and List) ---
        left_panel = wx.Panel(splitter) # Parent the panel to the splitter
        vbox_left = wx.BoxSizer(wx.VERTICAL)
        
        url_input_sizer = wx.BoxSizer(wx.HORIZONTAL)
        url_label = wx.StaticText(left_panel, label="URL:")
        url_input_sizer.Add(url_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.url_text = wx.TextCtrl(left_panel, size=(250, -1))
        url_input_sizer.Add(self.url_text, 1, wx.EXPAND | wx.RIGHT, 15)
        
        interval_label = wx.StaticText(left_panel, label="Check Interval (sec):")
        url_input_sizer.Add(interval_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.interval_spin = wx.SpinCtrlDouble(left_panel, value="3600", min=10, max=86400, inc=10)
        self.interval_spin.SetDigits(0)
        url_input_sizer.Add(self.interval_spin, 0, wx.ALIGN_CENTER_VERTICAL)
        
        vbox_left.Add(url_input_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        selector_input_sizer = wx.BoxSizer(wx.HORIZONTAL)
        tag_label = wx.StaticText(left_panel, label="Tag:")
        selector_input_sizer.Add(tag_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.tag_text = wx.TextCtrl(left_panel, size=(50, -1))
        selector_input_sizer.Add(self.tag_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        
        selector_type_label = wx.StaticText(left_panel, label="Selector Type:")
        selector_input_sizer.Add(selector_type_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.selector_type_combo = wx.ComboBox(left_panel, choices=['', 'id', 'class'], style=wx.CB_READONLY, size=(80,-1))
        selector_input_sizer.Add(self.selector_type_combo, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        selector_value_label = wx.StaticText(left_panel, label="Selector Value:")
        selector_input_sizer.Add(selector_value_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.selector_value_text = wx.TextCtrl(left_panel, size=(120, -1))
        
        selector_input_sizer.Add(self.selector_value_text, 1, wx.EXPAND )
        
        vbox_left.Add(selector_input_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.add_button = wx.Button(left_panel, label="Add/Update URL")
        self.Bind(wx.EVT_BUTTON, self.on_add_url, self.add_button)
        button_sizer.Add(self.add_button, 0, wx.RIGHT, 10)
        
        self.delete_button = wx.Button(left_panel, label="Delete Selected")
        self.Bind(wx.EVT_BUTTON, self.on_delete_url, self.delete_button)
        button_sizer.Add(self.delete_button, 0, wx.RIGHT, 10)
        
        self.start_button = wx.Button(left_panel, label="Start Monitoring")
        self.Bind(wx.EVT_BUTTON, self.on_start_monitoring, self.start_button)
        button_sizer.Add(self.start_button, 0, wx.RIGHT, 10)
        
        self.stop_button = wx.Button(left_panel, label="Stop Monitoring")
        self.Bind(wx.EVT_BUTTON, self.on_stop_monitoring, self.stop_button)
        self.stop_button.Enable(False)
        button_sizer.Add(self.stop_button, 0)
        
        vbox_left.Add(button_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)
        
        self.url_list = wx.ListCtrl(left_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        
        self.url_list.InsertColumn(0, 'URL', width=180) # Adjusted width for splitter
        self.url_list.InsertColumn(1, 'Interval (sec)', width=80)
        self.url_list.InsertColumn(2, 'Enabled', width=60)
        self.url_list.InsertColumn(3, 'Monitored Element', width=120)
        self.url_list.InsertColumn(4, 'Last Check', width=90)
        self.url_list.InsertColumn(5, 'Last Change', width=90)
        self.url_list.InsertColumn(6, 'Status', width=140)
        
        self.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_url_selected, self.url_list)
        self.Bind(wx.EVT_LIST_ITEM_DESELECTED, self.on_url_deselected, self.url_list)
        self.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_url_activated, self.url_list) # Keep this for now
        
        vbox_left.Add(self.url_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        left_panel.SetSizer(vbox_left)
        
        
        # --- Right Panel (WebView) ---
        self.webview_panel = wx.Panel(splitter) # Parent the panel to the splitter
        webview_sizer = wx.BoxSizer(wx.VERTICAL)
        
        webview_label = wx.StaticText(self.webview_panel, label="Monitored Page View:")
        webview_sizer.Add(webview_label, 0, wx.ALL | wx.EXPAND, 5)
        
        if hasattr(wx.html2, 'WebView'):
             self.webview = wx.html2.WebView.New(self.webview_panel)
             if not self.webview:
                 wx.MessageBox("WebView could not be created. Ensure you have a compatible backend installed (e.g., WebKitGTK, Edge, etc.).", "WebView Error", wx.OK | wx.ICON_ERROR)
                 print("FATAL: WebView could not be created. Please check your wxPython installation and environment.")
                 self.disable_webview_features()
             else:
                  self.webview.Bind(wx.html2.EVT_WEBVIEW_LOADED, self.on_webview_load_completed)
                  self.webview.Bind(wx.html2.EVT_WEBVIEW_ERROR, self.on_webview_load_failed)
             webview_sizer.Add(self.webview, 1, wx.EXPAND | wx.ALL, 5)
        else:
            placeholder = wx.StaticText(self.webview_panel, label="wx.html2.WebView is not available in this wxPython build or environment.")
            webview_sizer.Add(placeholder, 1, wx.EXPAND | wx.ALL, 5)
            print("Warning: wx.html2.WebView is not available. Monitoring disabled.")
            self.disable_webview_features()
            
        self.webview_panel.SetSizer(webview_sizer)
        splitter.SplitVertically(left_panel, self.webview_panel)
        initial_sash_position = 600 # Start with the left panel about 600 pixels wide
        splitter.SetSashPosition(initial_sash_position)
      main_sizer.Add(splitter, 1, wx.EXPAND | wx.ALL, 0) # Splitter takes all available space
        
        panel.SetSizer(main_sizer)
        main_sizer.Fit(self) # Fit the frame to the sizers
        # --- Status Bar ---
        self.CreateStatusBar()
        self.GetStatusBar().SetStatusText("Ready")


    def disable_webview_features(self):
        """Helper to disable WebView-dependent features if it fails to create."""
        self.start_button.Enable(False)
        self.GetStatusBar().SetStatusText("WebView not available. Monitoring disabled.")
        if self.webview_panel:
            sizer = self.webview_panel.GetSizer()
            if sizer:
                 # Optionally hide the WebView control itself if it exists
                 if self.webview:
                      self.webview.Hide()
                 sizer.Layout() # Update layout after hiding
                 self.Layout() # Update frame layout
                 
    def escape_attribute_value(self, value):
        """Escape single quotes in the attribute value for use in JavaScript."""
        return value.replace("'", "\\'")


    def on_add_url(self, event):
        url = self.url_text.GetValue().strip()
        interval = int(self.interval_spin.GetValue())
        tag = self.tag_text.GetValue().strip()
        selector_type = self.selector_type_combo.GetValue()
        selector_value = self.selector_value_text.GetValue().strip()

        if not url:
            wx.MessageBox("Please enter a URL.", "Input Error", wx.OK | wx.ICON_ERROR)
            return

        if tag and (not selector_type or not selector_value):
             wx.MessageBox("If you specify a Tag, you must also specify a Selector Type (id/class) and Selector Value.", "Input Error", wx.OK | wx.ICON_ERROR)
             return

        if selector_type and not (tag and selector_value):
             wx.MessageBox("If you specify a Selector Type (id/class), you must also specify a Tag and Selector Value.", "Input Error", wx.OK | wx.ICON_ERROR)
             return

        # Add http:// if not present, simple check
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'http://' + url # Default to http

        if url in self.urls_to_monitor:
            # Ask user if they want to update
            monitor_to_update = self.urls_to_monitor[url]
            if wx.MessageBox(f"URL '{url}' already exists. Do you want to update its settings?", "Update URL", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
                 monitor_to_update.interval = interval
                 monitor_to_update.tag = tag
                 monitor_to_update.selector_type = selector_type
                 monitor_to_update.selector_value = selector_value
                 monitor_to_update.enabled = True # Assume update means enabling

                 # Optional: If important URL updated, maybe reset its state?
                 # monitor_to_update.last_check_time = 0
                 # monitor_to_update.last_source = ""
                 # monitor_to_update.last_change_time = None

                 self.GetStatusBar().SetStatusText(f"URL settings updated for {url}")
            else:
                 return # User cancelled update

        else:
            # Add new URL
            new_monitor = URLMonitor(url, interval, tag=tag, selector_type=selector_type, selector_value=selector_value)
            self.urls_to_monitor[url] = new_monitor
            self.GetStatusBar().SetStatusText(f"URL added: {url}")

        self.update_list_ctrl()
        self.save_data()


    def on_delete_url(self, event):
        selected_index = self.url_list.GetFirstSelected()
        if selected_index == -1:
            wx.MessageBox("Please select a URL to delete.", "Delete Error", wx.OK | wx.ICON_ERROR)
            return

        url_to_delete = self.url_list.GetItemText(selected_index, 0)

        if wx.MessageBox(f"Are you sure you want to delete '{url_to_delete}'?", "Confirm Delete", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            if url_to_delete in self.urls_to_monitor:
                # Remove from check queue if present
                self.check_queue = [u for u in self.check_queue if u != url_to_delete]
                del self.urls_to_monitor[url_to_delete]
                self.update_list_ctrl()
                self.save_data()
                self.GetStatusBar().SetStatusText(f"URL deleted: {url_to_delete}")
            else:
                 self.GetStatusBar().SetStatusText(f"Error: URL not found in internal list: {url_to_delete}")


    def on_url_selected(self, event):
        selected_index = self.url_list.GetFirstSelected()
        if selected_index != -1:
            url = self.url_list.GetItemText(selected_index, 0)
            if url in self.urls_to_monitor:
                monitor = self.urls_to_monitor[url]
                self.url_text.SetValue(monitor.url)
                self.interval_spin.SetValue(monitor.interval)
                self.tag_text.SetValue(monitor.tag)
                self.selector_type_combo.SetValue(monitor.selector_type)
                self.selector_value_text.SetValue(monitor.selector_value)
                self.add_button.SetLabel("Update Selected URL")

    def on_url_deselected(self, event):
        if self.url_list.GetFirstSelected() == -1:
            self.url_text.Clear()
            self.add_button.SetLabel("Add/Update URL")

    def on_url_activated(self, event):
        selected_index = self.url_list.GetFirstSelected()
        if selected_index != -1:
            url = self.url_list.GetItemText(selected_index, 0)
            if self.webview and hasattr(self.webview, 'LoadURL'):
                 print(f"Loading {url} in WebView...")
                pass # Decide if we want this feature and how to implement it safely

    def on_start_monitoring(self, event):
        if not self.webview or not hasattr(self.webview, 'LoadURL'):
             wx.MessageBox("Monitoring requires a working WebView.", "Error", wx.OK | wx.ICON_ERROR)
             return # Prevent starting if WebView failed

        if not self.monitoring_thread or not self.monitoring_thread.is_alive():
            print("Starting monitoring thread...")
            self.monitoring_running = True
            # Clear the queue on start to prevent processing old requests
            self.check_queue = [] 
            self.monitoring_thread = threading.Thread(target=self.monitor_urls_thread)
            self.monitoring_thread.daemon = True
            self.monitoring_thread.start()
            self.start_button.Enable(False)
            self.stop_button.Enable(True)
            self.GetStatusBar().SetStatusText("Monitoring started...")
        else:
            self.GetStatusBar().SetStatusText("Monitoring is already running.")
            print("Monitoring thread already running.")


    def on_stop_monitoring(self, event):
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            print("Stopping monitoring thread...")
            self.monitoring_running = False
            self.check_queue = [] 

             # Wait for the thread to finish
            self.monitoring_thread.join(timeout=5)

            if self.monitoring_thread.is_alive():
                print("Monitoring thread did not stop within timeout.")
                self.GetStatusBar().SetStatusText("Monitoring stopping (thread unresponsive)...")
            else:
                print("Monitoring thread stopped.")
                self.GetStatusBar().SetStatusText("Monitoring stopped.")

            # Reset WebView state if it was loading
            if self.webview_loading_url:
                 print(f"Monitoring stopped while loading {self.webview_loading_url}")
                 self.webview_loading_url = None
                 if self.webview and hasattr(self.webview, 'Stop'):
                      try:
                          self.webview.Stop()
                      except Exception as e:
                           print(f"Error stopping webview: {e}")


            self.start_button.Enable(True)
            self.stop_button.Enable(False)
        else:
            self.GetStatusBar().SetStatusText("Monitoring is not running.")
            print("Monitoring thread not running.")

    # --- WebView Events (Run on UI Thread) ---

    def on_request_webview_load(self, event):
         """Handler for the custom event from the monitoring thread."""
         url_to_load = event.url # Get the URL from the event object

         if not self.webview or not hasattr(self.webview, 'LoadURL'):
             print(f"WebView not available, cannot load {url_to_load}.")
             return

         if self.webview_loading_url:
             print(f"WebView busy ({self.webview_loading_url}), queueing {url_to_load}")
             if url_to_load not in self.check_queue: # Avoid duplicates
                 self.check_queue.append(url_to_load)
         else:
             # WebView is free, load the URL
             print(f"Loading {url_to_load} in WebView...")
             self.webview_loading_url = url_to_load
             self.update_url_status(url_to_load, "Loading...") # Update status in UI
             try:
                 self.webview.LoadURL(url_to_load)
             except Exception as e:
                 print(f"Error calling LoadURL for {url_to_load}: {e}")
                 self.webview_loading_url = None # Release the lock
                 # Post a failure event back to self
                 event = WebViewLoadFailedEvent(url=url_to_load, error=str(e))
                 wx.PostEvent(self, event)


    def on_webview_load_started(self, event):
         """Event handler for when the WebView starts loading."""
         url = event.GetURL()
         print(f"WebView started loading: {url}")
         pass # Status already set to "Loading..."


    def on_webview_load_completed(self, event):
        """Event handler for when the WebView finishes loading."""
        loaded_url = event.GetURL()
        print(f"WebView finished loading: {loaded_url}")
 
        original_url_requested = self.webview_loading_url
 
        if original_url_requested not in self.urls_to_monitor:
             print(f"Completed load for unknown or deleted URL: {original_url_requested}")
             self.webview_loading_url = None
             self.process_next_webview_load()
             return
 
        monitor = self.urls_to_monitor[original_url_requested]
 
        status = "Processing..."
        self.update_url_status(original_url_requested, status) # Update UI immediately to "Processing..." state
 
 
        try:
            # Use JavaScript to retrieve the element content
            escaped_attribute_value = self.escape_attribute_value(monitor.selector_value)
 
            js_script = f"""
             (function() {{
                 try {{
                     // Use querySelector for robustness across id/class etc.
                     var element = document.querySelector("{monitor.tag}[{monitor.selector_type}='{escaped_attribute_value}']");
                     if (element) {{
                         var text = (element.textContent || '').trim();
                         return JSON.stringify({{ found: true, content: text }});
                     }} else {{
                         return JSON.stringify({{ found: false }});
                     }}
                 }} catch(e) {{
                     return JSON.stringify({{ error: true, message: e.message }});
                 }}
             }})();
             """
            runscript_result = self.webview.RunScript(js_script)  # store this to inspect better later
 
            success, js_result_str = runscript_result  # Unpack the tuple.  CRITICAL STEP.
 
            if not success:
                print(f"WebView.RunScript failed for {original_url_requested}")
                status = "JavaScript Error: RunScript failed"
                self.update_url_status(original_url_requested, status)
                self.webview_loading_url = None
                self.process_next_webview_load()
                return 
 
            try:
                js_result = json.loads(js_result_str)
 
 
                if "error" in js_result and js_result["error"]:
                    raise Exception(f"JavaScript error: {js_result.get('message', 'Unknown error')}")
 
                if js_result.get("found", False):
                    element_content = js_result.get("content", "")
                else:
                    element_content = None  # Element not found
                    print(f"Element {monitor.tag}[{monitor.selector_type}='{monitor.selector_value}'] not found on {original_url_requested}")
                    if monitor.last_source != "":
                        # If previously found, finding nothing is potentially a change state
                        status = "Element not found (potential change?)" # Set status here
 
                    else:
                        # If never found before, just update status and finish for this URL
                        status = "Element not found" # Set status here
                        # Update UI explicitly here if we exit early
                        self.update_url_status(original_url_requested, status)
                        # Finished with this load - release lock and process next
                        self.webview_loading_url = None
                        self.process_next_webview_load()
                        return # Exit this handler early
 
 
            except json.JSONDecodeError as e:
                 print(f"Error decoding JSON from JavaScript: {e}.  Raw JS result: {js_result_str}")
                 status = f"JSON Decode Error: {e}"
                 self.update_url_status(original_url_requested, status)
                 self.webview_loading_url = None
                 self.process_next_webview_load()
                 return 
 
 
            monitor.last_check_time = time.time()
 
            if element_content is None: # Element not found
                if monitor.last_source != "":
 
                    print(f"Change detected (element disappeared) for {original_url_requested}")
                    monitor.last_source = "" # Element has disappeared
 
                    monitor.last_change_time = monitor.last_check_time # Timestamp of the change
                    status = "Change Detected: Element Disappeared!" # Set status here
                    self.on_change_detected(original_url_requested) # Trigger notification and UI update
                else:
                    print(f"Element never found for {original_url_requested}")
                    # Nothing to see here
                    if status == "Processing...":
                          status = "Ok"
 
            elif monitor.last_source != element_content:
                print(f"Change detected for {original_url_requested}")
                monitor.last_source = element_content # Store the NEW content
                monitor.last_change_time = monitor.last_check_time # Timestamp of the change
                status = "Change Detected!" # Set status here
                self.on_change_detected(original_url_requested) # Trigger notification and UI update
            else:
                print(f"No change detected for {original_url_requested}")
                # If no change and status wasn't set because element wasn't found, set it to Ok
                # The 'Element not found (potential change?)' case also falls here if last_source != ""
                if status == "Processing..." or status == "Element not found (potential change?)":
                    status = "Ok" # Or "No change detected." # Set status here
 
 
            # Always call update_url_status with the FINAL determined status
            # This will overwrite the initial "Processing..." status
            self.update_url_status(original_url_requested, status)
            self.save_data() # Save state after a check completes
 
 
        except Exception as e:
            # This outer except block would catch errors from RunScript()
            # (more likely, but good practice)
            print(f"Unexpected error in webview load completed for {original_url_requested}: {e}")
            status = f"Internal Error: {e}" # Set status here
            self.update_url_status(original_url_requested, status)
 
 
        finally:
            # This block always runs after the try/except (and inner try/except)
            # Release the WebView lock and process the next item in the queue
            self.webview_loading_url = None
            self.process_next_webview_load()
            
            

    def on_webview_load_failed(self, event):
        """Event handler for WebView load errors."""
        failed_url = event.GetURL() # Might be the URL that failed
        error_desc = event.GetErrorDescription() if hasattr(event, 'GetErrorDescription') else "Unknown error"
        
        url_requested = self.webview_loading_url

        print(f"WebView failed to load {failed_url} (requested: {url_requested}) - Error: {error_desc}")


        try:
            if url_requested in self.urls_to_monitor:
                 monitor = self.urls_to_monitor[url_requested]
                 monitor.last_check_time = time.time() # Record the attempt time
                 status = f"Load Failed: {error_desc[:100]}..." # Truncate error message
                 self.update_url_status(url_requested, status) # Update status in UI
                 self.save_data() # Save state after an attempt

            else:
                 print(f"Load failed for unknown or deleted URL: {url_requested}")
        finally:
             # Release the WebView lock regardless of success or failure
             self.webview_loading_url = None
             self.process_next_webview_load() # Process the next item

    def process_next_webview_load(self):
         """Checks if there's a URL waiting in the queue and loads it."""
         if self.monitoring_running and not self.webview_loading_url and self.check_queue:
              next_url = self.check_queue.pop(0) # Get the next URL from the front of the queue
              print(f"Processing next URL from queue: {next_url}")
              # Request the load via CallAfter to ensure it happens correctly on the UI thread
              event = RequestWebViewLoadEvent(url=next_url)
              wx.PostEvent(self, event)
         elif not self.monitoring_running:
              print("Monitoring stopped, clearing WebView queue.")
              self.check_queue = []
         # If queue is empty or webview is busy, do nothing until next request or load completes


    # --- Monitoring Thread Logic ---

    def monitor_urls_thread(self):
        """Background thread function to periodically schedule WebView checks."""
        print("Monitor thread started.")
        while self.monitoring_running:
            # Find URLs that are due for a check
            urls_due_for_check = [
                url for url, monitor in self.urls_to_monitor.items()
                if monitor.should_check()
            ]

            for url in urls_due_for_check:
                 if url not in self.check_queue:
                     self.check_queue.append(url)
                     print(f"Added {url} to check queue.")

            # This logic is slightly redundant with process_next_webview_load being called
            # in completed/failed handlers, but ensures we start loading if the queue
            # has items and the WebView is initially free.
            if self.monitoring_running and not self.webview_loading_url and self.check_queue:
                 self.process_next_webview_load()


            # Determine the sleep time
            # We need to wake up when any enabled monitor is due for its next check
            next_check_time = float('inf')
            active_monitors = [m for m in self.urls_to_monitor.values() if m.enabled]

            if active_monitors:
                current_timestamp = time.time()
                for monitor in active_monitors:
                   # Calculate when the monitor should be checked next based on its interval
                   scheduled_next_check = monitor.last_check_time + monitor.interval
                   # Time remaining until this monitor is due
                   remaining_time = scheduled_next_check - current_timestamp
                   # Find the minimum positive remaining time among all monitors
                   if remaining_time > 0:
                       next_check_time = min(next_check_time, remaining_time)

                # If any monitors are due now or in the past, the smallest effective wait is very short
                if next_check_time == float('inf'): # No future checks scheduled (all are due now or in the past)
                    next_check_time = 1 # Check again soon

            else:
                 # No active monitors, sleep longer
                 print("No active monitors. Sleeping longer.")
                 next_check_time = 60 # Sleep 60 seconds if nothing is active

            # Ensure sleepy time is positive and not excessively long
            sleep_duration = max(1, min(next_check_time, 600)) # Sleep at least 1 sec, max 10 min


            print(f"Monitoring thread sleeping for ~{int(sleep_duration)} seconds. Queue length: {len(self.check_queue)}")

            # Sleep elegantly, checking the shutdown signal frequently
            sleep_increment = 1
            slept_time = 0
            while self.monitoring_running and slept_time < sleep_duration:
                 time.sleep(sleep_increment)
                 slept_time += sleep_increment


        print("Monitor thread stopping cleanly.")


    def update_url_status(self, url, status_text):
        """Update the status column for a specific URL row."""
        for i in range(self.url_list.GetItemCount()):
            if self.url_list.GetItemText(i, 0) == url:
                monitor = self.urls_to_monitor.get(url) # Get URLMonitor object

                # Ensure monitor exists before accessing attributes
                if monitor:
                     last_check_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(monitor.last_check_time)) if monitor.last_check_time else "Never"
                     last_change_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(monitor.last_change_time)) if monitor.last_change_time else "None"

                     self.url_list.SetItem(i, 4, last_check_str)
                     self.url_list.SetItem(i, 5, last_change_str)
                     self.url_list.SetItem(i, 6, status_text)
                break # Found the URL, exit loop


    def update_list_ctrl(self):
        """Clears and repopulates the ListCtrl from urls_to_monitor."""
        # Store selected index if any to re-select after update
        selected_url = None
        selected_index = self.url_list.GetFirstSelected()
        if selected_index != -1:
             selected_url = self.url_list.GetItemText(selected_index, 0)


        self.url_list.DeleteAllItems()
        index = 0
        for url, monitor in self.urls_to_monitor.items():
            monitored_element_info = ""
            if monitor.tag and monitor.selector_type and monitor.selector_value:
                 monitored_element_info = f"{monitor.selector_type}={monitor.selector_value} ({monitor.tag})"
            else:
                 monitored_element_info = "Entire Page"

            self.url_list.InsertItem(index, url)
            self.url_list.SetItem(index, 1, str(monitor.interval))
            self.url_list.SetItem(index, 2, "Yes" if monitor.enabled else "No")
            self.url_list.SetItem(index, 3, monitored_element_info)
            # Use existing monitor data if available, otherwise default display
            last_check_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(monitor.last_check_time)) if monitor.last_check_time else "Never"
            last_change_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(monitor.last_change_time)) if monitor.last_change_time else "None"
            self.url_list.SetItem(index, 4, last_check_str)
            self.url_list.SetItem(index, 5, last_change_str)
             # Initial status or loaded status
            status = "Idle"
            if url in self.urls_to_monitor and self.urls_to_monitor[url].last_check_time > 0:
                 pass # Status will be updated by monitoring process
            self.url_list.SetItem(index, 6, status)
            index += 1

        # Re-select the item if one was selected before update
        if selected_url:
             for i in range(self.url_list.GetItemCount()):
                  if self.url_list.GetItemText(i, 0) == selected_url:
                       self.url_list.Select(i)
                       break


    def update_list_ctrl_row(self, url):
         """Updates a specific row in the ListCtrl for a given URL."""
         for i in range(self.url_list.GetItemCount()):
             if self.url_list.GetItemText(i, 0) == url:
                 monitor = self.urls_to_monitor.get(url)
                 if monitor:
                    monitored_element_info = ""
                    if monitor.tag and monitor.selector_type and monitor.selector_value:
                         monitored_element_info = f"{monitor.selector_type}={monitor.selector_value} ({monitor.tag})"
                    else:
                         monitored_element_info = "Entire Page"

                    self.url_list.SetItem(i, 1, str(monitor.interval))
                    self.url_list.SetItem(i, 2, "Yes" if monitor.enabled else "No")
                    self.url_list.SetItem(i, 3, monitored_element_info)
                    last_check_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(monitor.last_check_time)) if monitor.last_check_time else "Never"
                    last_change_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(monitor.last_change_time)) if monitor.last_change_time else "None"
                    self.url_list.SetItem(i, 4, last_check_str)
                    self.url_list.SetItem(i, 5, last_change_str)
                break 

    def on_change_detected(self, url):
        """Method called by the monitoring thread or WebView handler via wx.CallAfter on change."""
        if url in self.urls_to_monitor:
            monitor = self.urls_to_monitor[url]
            self.update_url_status(url, "Change Detected!")
            self.show_notification(f"Change Detected on {url}", f"The monitored content on {url} has changed.")


    def load_data(self):
        """Loads URLs and settings from a pickle file."""
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'rb') as f:
                    data = pickle.load(f)
                    if isinstance(data, dict):
                        self.urls_to_monitor = {url: monitor for url, monitor in data.items() if isinstance(monitor, URLMonitor)}
                        print(f"Loaded {len(self.urls_to_monitor)} URLs from {DATA_FILE}")
                    else:
                        print(f"Data in {DATA_FILE} is not a dictionary, starting fresh.")
                        self.urls_to_monitor = {}

            except Exception as e:
                print(f"Error loading data from {DATA_FILE}: {e}")
                self.urls_to_monitor = {}
        else:
            print(f"No data file found at {DATA_FILE}")


    def save_data(self):
        """Saves current URLs and settings to a pickle file."""
        try:
            with open(DATA_FILE, 'wb') as f:
                pickle.dump(self.urls_to_monitor, f)
            print(f"Saved {len(self.urls_to_monitor)} URLs to {DATA_FILE}")
        except Exception as e:
            print(f"Error saving data to {DATA_FILE}: {e}")


    def show_notification(self, title, message):
        """Displays a native desktop notification."""
        try:
           # Check for availability of NotificationMessage before using it
           notification = wx.adv.NotificationMessage(title, message)
           notification.Show()            
        except Exception as e:
             print(f"Error showing notification: {e}")


    def on_close(self, event):
        """Handler for the window close event."""
        print("Main frame closing.")
        self.monitoring_running = False

        # Clear queue and WebView state on close
        self.check_queue = []
        self.webview_loading_url = None
         # Attempt to stop current webview load if any
        if self.webview and hasattr(self.webview, 'Stop'):
            try:
                self.webview.Stop()
            except Exception as e:
                print(f"Error stopping webview on close: {e}")


        if self.monitoring_thread and self.monitoring_thread.is_alive():
             print("Waiting for monitoring thread to join...")
             self.monitoring_thread.join(timeout=5)

             if self.monitoring_thread.is_alive():
                print("Monitoring thread did not exit gracefully.")

        self.save_data()
        self.Destroy()


# --- Custom Event Class for inter-thread communication ---
# Needed to pass data (like the URL) with the event
class WebViewLoadEvent(wx.PyCommandEvent):
    """Custom event for WebView load requests and completions."""
    def __init__(self, etype, eid=0, url="", error=""):
        super().__init__(etype, eid)
        self.url = url
        self.error = error # For failure event


# Assign specific event types to the custom events
RequestWebViewLoadEvent, EVT_REQUEST_WEBVIEW_LOAD = wx.lib.newevent.NewEvent()
WebViewLoadCompletedEvent, EVT_WEBVIEW_LOAD_COMPLETED = wx.lib.newevent.NewEvent()
WebViewLoadFailedEvent, EVT_WEBVIEW_LOAD_FAILED = wx.lib.newevent.NewEvent()


# --- Application Entry Point ---
if __name__ == '__main__':
    app = wx.App(False)
    frame = AppFrame(None, title=APP_NAME)
    app.MainLoop()
