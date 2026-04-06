# llama-launcher
Python launcher for Llama.cpp

That was built using Claude Code with two prompts:

The initial prompt:


> This is a blank environment ready for you to start work in. A virtual environment is created and wxPython has been installed. Build the file llama_launcher.py. This is a Python file that uses the wxPython GUI toolkit to allow for easy selection of a local LLM and easy launching of llama-server.exe. The primary interface should allow the user to choose a folder from the models directory (the location of this directory is configurable in the interface). This folder may contain a single .gguf file, in which case that is the model to be specified in the call to llama-server.exe using the -m parameter. The folder may also contain two gguf files, in which case one MUST have mmproj in the file name and one MUST NOT have mmproj in the file name. In this case, the file WITHOUT mmproj is the model, and the file WITH mmproj is the multimodal projection file, to be passed to llama-server with the --mmproj parameter. If the model folder does not meet one of these two criteria, it is not valid and should be displayed in a different color and with a description of the error. Directories for the llama.cpp installation and for models should be configurable by entering a path or using a file browser. Server port, image-min-tokens and image-max-tokens should be configurable. Configuration should be stored in a config.json file located in the same folder as llama_launcher.py by default, and optionally located in a different folder based on a --config argument passed to llama_launcher.py. Begin work on the initial build and then I will provide feedback and additional features to be implemented.


This resulted in a working application. An extra feature was added to check for existing servers running on the designated port, using the following prompt:

> This is almost perfect. Let's remove the pop-up dialog box after launching llama-server.py since it will create a console window indicating that the server has been launched. Let's add a check that attempts an HTTP request on 127.0.0.1 on the selected port when clicking Launch and if it finds a server already running, it displays a pop-up stating that the server is already running on that port.

The code was briefly reviewed for any unexpected behavior. No code was manually written for this project.

There are a few things I would change, such as the min/max token values and the indent value of config.json (I prefer 4). But the goal of this project was to test implementing a small utility with zero lines of code written or modified.

TODO:

- Review for possible additional features after using the application
