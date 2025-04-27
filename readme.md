## Deploying the app
1. Ensure the .env file is present in the root directory.
2. Also ensure that azure web app is created with python version 3.11 and with code deployment mode
3. Allow the FTP based deployment in the deployment center of the web app.
4. Ensure this setting is set in the web app:
 - SCM_DO_BUILD_DURING_DEPLOYMENT=true
5. Create the app deployment package i.e. zip by selecting the requriements.txt file and the app.py file.
6. Upload the zip file to the Azure App Service using command below:
```
az webapp deploy --resource-group ms-hack --name appmshack01 --src-path "D:\Events\MS-Hack\app.zip"
```

