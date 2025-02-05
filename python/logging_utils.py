from colorama import init, Fore, Style
import logging

def setup_logging():
    """Configure logging with colored output for console and file output"""
    # Initialize colorama
    init()

    # Custom formatter with colors for console
    class ColorFormatter(logging.Formatter):
        format_str = '%(name)s || %(levelname)s || %(message)s'

        FORMATS = {
            logging.NOTSET: Fore.WHITE + format_str + Style.RESET_ALL,
            logging.DEBUG: Fore.WHITE + format_str + Style.RESET_ALL,
            logging.INFO: Fore.CYAN + format_str + Style.RESET_ALL,
            logging.WARNING: Fore.YELLOW + format_str + Style.RESET_ALL,
            logging.ERROR: Fore.RED + format_str + Style.RESET_ALL,
            logging.CRITICAL: Fore.RED + Style.BRIGHT + format_str + Style.RESET_ALL,
            logging.FATAL: Fore.RED + Style.BRIGHT + format_str + Style.RESET_ALL,
        }

        def format(self, record):
            log_fmt = self.FORMATS.get(record.levelno, self.FORMATS[logging.INFO])
            formatter = logging.Formatter(log_fmt)
            return formatter.format(record)

    # Set up console handler with color formatter
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(ColorFormatter())

    # Set up file handler with standard formatting (no colors)
    file_handler = logging.FileHandler('0xbuilder.log')
    file_handler.setFormatter(
        logging.Formatter('%(name)s || %(levelname)s || %(message)s')  # Corrected placeholder
    )

    # Configure the root logger with both handlers
    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[console_handler, file_handler]
    )
    
    
