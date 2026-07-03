CREATE TABLE [Person].[BusinessEntityAddress] (
    [BusinessEntityID] int NOT NULL,
    [AddressID] int NOT NULL,
    [AddressTypeID] int NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
