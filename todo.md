Archive Helper for Jellyfin Task List
--------------------------------------

Look into OMDB API Key

-------------------

I have a few Movie DVD's that contain 4 movies on a Single DVD. I would like to add a check box for Multiple Titles for DVD. If it's checked allow the User to enter up to 4 movie titles and years and the MKV's extracted in order will be labeled with those titles.

--------------------

Add the ability to upload Books - Integrate Good Reads data to see if some of the Meta data can be pulled. I wonder how Calibre gets the Meta data for books?
--Subtask see if it's possible to pull books from Kindle and Convert them to epub and add them to the library.

Add the ability to pull Audible Books from your personal Audible collection convert them to MP3 and add them to the Jellyfin Library.

Add the ability to Rip and Classify Music CD's.

Look into auto generating cover.jpg or cover.png

Look into generating metadata.opf file for each book to help indexing.

Could we use bash command DVD_NAME=$(blkid -o value -s LABEL /dev/sr0) on the server to get a name of the movie and compare to what's expected in the list for a sanity check to make sure that the expected dvd is in the drive.

-------------

This system was designed for a spare laptop or desktop that has a DVD-ROM installed sitting around that you want to through some disks in and use as a Jellyfin server or even using a Raspberry Pi with a USB DVD-ROM. This script wasn't designed for renting a remote server that you don't have physical access to in my opinion that defeats the purpose the idea in my mind is to allow access to your physical media without the work of digging through it all for one movie, it also reduced wear and tear on your dvd collection. Big reason for me prevents the kids from digging around the DVD's and breaking them by mistake or getting the dreaded syrup from breakfast all over a disc. If you know you know.

